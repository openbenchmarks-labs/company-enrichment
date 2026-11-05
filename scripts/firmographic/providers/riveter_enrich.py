"""Riveter enrichment adapter using the shared web-research enrichment contract.

Riveter fills output columns for rows of input data: one AI agent per row
researches every column together with web search, page reads, and LinkedIn
company lookups as tools, and reports the URL it used for each value. The
recipe (task prompt, the ten typed output columns of the shared contract, the
per-row credit budget, the tools) is saved once per process with
POST /v1/enrichments (free), then each case runs as one row through
POST /v1/enrich; the filled cells and their source URLs are read back from the
run's dataset. API reference: https://docs.riveterhq.com
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque
from typing import Any

import requests

from firmographic.common import CompanyCase, ProviderResult
from firmographic.web_research import (
    OUTPUT_SCHEMA,
    SCORING_FIELDS,
    has_scored_value,
    normalized_company,
    parse_structured_value,
    request_contract,
)

from .base import ProviderHTTPError, request_json


VENDOR_SLUG = "riveter-enrich"
VENDOR_NAME = "Riveter"
API_BASE = "https://api.riveterhq.com/v1"
REQUIRED_ENV = ("RIVETER_API_KEY",)
PAID_CALLS_PER_CASE = 1
USER_AGENT = "openbenchmarks-company-enrichment/1.0"
# Self-Serve list price per credit, https://www.riveterhq.com/pricing, checked 2026-11-04.
# The run reports credits; a dollar figure is this times credits_used.
USD_PER_CREDIT = 0.025
PRICING_URL = "https://www.riveterhq.com/pricing"
# Credits the row agent may spend on one case. Riveter bills min(spent, budget); its default is 1.5 per output column.
BUDGET_CREDITS_PER_ROW = 30
# LinkedIn company tools on top of Riveter's default toolset (web search, page reads, PDF and image reading,
# HTTP, code, and the LinkedIn company data lookup). Ones the account cannot use are left off the recipe.
ADDITIONAL_TOOLS = ("linkedin_company_profile", "linkedin_company_job_count", "linkedin_company_posts")
INPUT_NAME_COLUMN = "Company Name"
INPUT_DOMAIN_COLUMN = "Company Domain"
REQUEST_TIMEOUT_S = 90
POLL_WAIT_S = 50
POLL_FLOOR_S = 10
# A run given up on keeps billing while the rerun starts another; the agent's own bound is the credit budget.
POLL_DEADLINE_S = 3600
TERMINAL_STATUSES = {"success", "stopped", "failed", "error"}
# Riveter admits 30 kickoffs per minute per account and counts refused attempts; stay under it across threads.
KICKOFFS_PER_MINUTE = 28

# web_research.instruction(), written for a saved recipe: the case's name and domain are the row's input columns.
TASK_PROMPT = f"""Research the company identified by the website in the "{INPUT_DOMAIN_COLUMN}" column.
The supplied company name is in the "{INPUT_NAME_COLUMN}" column. Fill in a company-enrichment
record for that exact company. Do not substitute a parent, subsidiary, similarly named
company, or brand owner unless the supplied website itself identifies that entity.

Use reliable first-party or LinkedIn sources where available. Leave a column not found when
no source supports a value; do not guess. For headcount, return a range only when the source
supports a range. Record the URL you used for each value."""

COLUMN_PROMPTS = {
    "legal_name": (
        "The company's official legal or registered name, as its own website, legal pages, or LinkedIn "
        'page states it (for example "Example, Inc."). The entity at the input domain, not a parent or subsidiary.'
    ),
    "primary_domain": (
        'The company\'s canonical public website domain, lowercase, with no scheme, "www.", or path '
        '(for example "example.com"): the input domain, or the domain it now redirects to.'
    ),
    "hq_country": 'The country of the company\'s headquarters, as a country name in English (for example "United States").',
    "hq_city": "The city of the company's headquarters, the city name only, without state, region, or country.",
    "founded_year": "The four-digit year the company was founded, as an integer, only when a source states it.",
    "industry": (
        "The company's primary industry or business category as one short label, as its LinkedIn company page "
        'or website describes it (for example "Software Development").'
    ),
    "industries": (
        "Up to four industry labels that describe the company, the primary one first, each a short label as on "
        "its LinkedIn company page or website."
    ),
    "linkedin_url": (
        "The canonical LinkedIn company-page URL of this exact company, in the form "
        "https://www.linkedin.com/company/<slug>. Not a person, post, school, or showcase page, and not a "
        "parent's or a similarly named company's page: the page must belong to the company at the input domain."
    ),
    "headcount_min": (
        "The lower bound of the company's current employee count, as an integer: the low end of a supported "
        'range (for example 51 from "51-200 employees"), or the exact count when a source gives one.'
    ),
    "headcount_max": (
        "The upper bound of the company's current employee count, as an integer: the high end of a supported "
        'range (for example 200 from "51-200 employees"), or the exact count when a source gives one. '
        'Not found for an open-ended range such as "10,001+".'
    ),
}

_SAVED_ENRICHMENT: dict[str, Any] = {}
_SAVED_ENRICHMENT_LOCK = threading.Lock()
_KICKOFF_TIMES: deque[float] = deque()
_KICKOFF_LOCK = threading.Lock()


def column_schema(key: str) -> dict[str, Any]:
    """The contract's property for one column, without null: Riveter has a not-found state per cell."""
    contract = OUTPUT_SCHEMA["properties"][key]
    if contract["type"] == "array":
        return {"type": "array", "items": contract["items"]}
    return {"type": next(name for name in contract["type"] if name != "null")}


def recipe(additional_tools: list[str]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "OpenBenchmarks company enrichment (web research v1.0)",
        "prompt": TASK_PROMPT,
        "input_columns": [INPUT_NAME_COLUMN, INPUT_DOMAIN_COLUMN],
        "output": [
            {"name": key, "prompt": COLUMN_PROMPTS[key], "output_schema": column_schema(key)}
            for key in SCORING_FIELDS
        ],
        "budget": BUDGET_CREDITS_PER_ROW,
        "budget_locked": True,
    }
    if additional_tools:
        body["additional_tools"] = additional_tools
    return body


def kickoff_body(case: CompanyCase, enrichment_id: str) -> dict[str, Any]:
    return {
        "enrichment_id": enrichment_id,
        "input": {INPUT_NAME_COLUMN: [case.input_name or ""], INPUT_DOMAIN_COLUMN: [case.input_domain]},
    }


def headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {os.environ['RIVETER_API_KEY']}",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }


def enabled_additional_tools() -> list[str]:
    body, _ = request_json("GET", f"{API_BASE}/tools", headers=headers(), timeout=REQUEST_TIMEOUT_S)
    tools = body.get("tools") if isinstance(body, dict) else None
    enabled = {tool["name"] for tool in tools or [] if isinstance(tool, dict) and tool.get("enabled")}
    return [name for name in ADDITIONAL_TOOLS if name in enabled]


def saved_enrichment() -> dict[str, Any]:
    """The recipe saved on the account, created on the first call of the process (free, no run)."""
    with _SAVED_ENRICHMENT_LOCK:
        if not _SAVED_ENRICHMENT:
            body, _ = request_json(
                "POST",
                f"{API_BASE}/enrichments",
                headers=headers(),
                json_body=recipe(enabled_additional_tools()),
                timeout=REQUEST_TIMEOUT_S,
            )
            if not isinstance(body, dict) or not isinstance(body.get("id"), str):
                raise ValueError("Riveter did not return a saved enrichment")
            _SAVED_ENRICHMENT.update(body)
        return dict(_SAVED_ENRICHMENT)


def wait_for_kickoff_slot() -> None:
    """At most KICKOFFS_PER_MINUTE kickoffs in any 60 s window across the process's threads."""
    with _KICKOFF_LOCK:
        _drop_kickoffs_older_than_a_minute()
        if len(_KICKOFF_TIMES) >= KICKOFFS_PER_MINUTE:
            time.sleep(max(0.0, 60.0 - (time.monotonic() - _KICKOFF_TIMES[0])))
            _drop_kickoffs_older_than_a_minute()
        _KICKOFF_TIMES.append(time.monotonic())


def _drop_kickoffs_older_than_a_minute() -> None:
    now = time.monotonic()
    while _KICKOFF_TIMES and now - _KICKOFF_TIMES[0] >= 60.0:
        _KICKOFF_TIMES.popleft()


def poll_run(run_id: str) -> dict[str, Any]:
    """Long-poll the run to a terminal status, by wall clock: a queued run answers at once."""
    deadline = time.monotonic() + POLL_DEADLINE_S
    while True:
        started = time.monotonic()
        body = _poll_once(run_id)
        if isinstance(body, dict) and body.get("status") in TERMINAL_STATUSES:
            return body
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Riveter run {run_id} was not finished after {POLL_DEADLINE_S}s")
        time.sleep(max(0.0, POLL_FLOOR_S - (time.monotonic() - started)))


def _poll_once(run_id: str) -> Any:
    try:
        body, _ = request_json(
            "GET", f"{API_BASE}/runs/{run_id}", headers=headers(), params={"wait": POLL_WAIT_S}, timeout=REQUEST_TIMEOUT_S
        )
    except ProviderHTTPError as error:
        if error.status_code < 500 and error.status_code != 429:
            raise
        return None
    except requests.RequestException:
        return None
    return body


def dataset_row(dataset_id: str) -> dict[str, Any]:
    body, _ = request_json(
        "GET", f"{API_BASE}/datasets/{dataset_id}/rows", headers=headers(), params={"limit": 1}, timeout=REQUEST_TIMEOUT_S
    )
    rows = body.get("rows") if isinstance(body, dict) else None
    if not rows or not isinstance(rows[0], dict):
        raise ValueError(f"Riveter dataset {dataset_id} returned no row")
    return rows[0]


def parse_row(row: dict[str, Any]) -> dict[str, Any]:
    """The contract object from the row's cells: an `ok` cell is the value, anything else null; sources from the cells."""
    cells = row.get("cells")
    if not isinstance(cells, dict):
        raise ValueError("Riveter row had no cells")
    failed = sorted(key for key in SCORING_FIELDS if isinstance(cells.get(key), dict) and cells[key].get("state") == "failed")
    if failed:
        raise ValueError(f"Riveter left cells failed: {', '.join(failed)}")
    value: dict[str, Any] = {}
    sources: list[str] = []
    for key in SCORING_FIELDS:
        cell = cells.get(key)
        if not isinstance(cell, dict):
            raise ValueError(f"Riveter row had no cell for {key}")
        value[key] = cell.get("value") if cell.get("state") == "ok" else None
        sources.extend(url for url in cell.get("sources") or [] if isinstance(url, str) and url)
    value["industries"] = _as_list(value.get("industries"))
    return parse_structured_value({**value, "sources": list(dict.fromkeys(sources))})


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return [value] if value not in (None, "") else []


def finished_run(case: CompanyCase, enrichment_id: str) -> tuple[dict[str, Any], dict[str, Any], int]:
    """Kick off one row and wait for it. Returns (kickoff body, terminal run, latency_ms)."""
    body = kickoff_body(case, enrichment_id)
    wait_for_kickoff_slot()
    started = time.perf_counter()
    kickoff, _ = request_json("POST", f"{API_BASE}/enrich", headers=headers(), json_body=body, timeout=REQUEST_TIMEOUT_S)
    if not isinstance(kickoff, dict) or not isinstance(kickoff.get("id"), str):
        raise ValueError("Riveter did not return a run")
    if kickoff.get("legacy"):
        raise ValueError(f"Riveter ran the legacy pipeline: {kickoff['legacy']}")
    run_envelope = poll_run(kickoff["id"])
    latency_ms = round((time.perf_counter() - started) * 1000)
    if run_envelope.get("status") != "success":
        error = run_envelope.get("error") or {}
        reason = run_envelope.get("stopped_reason") or (error.get("message") if isinstance(error, dict) else None)
        raise ValueError(f"Riveter run {kickoff['id']} ended {run_envelope.get('status')}: {reason}")
    run_envelope.setdefault("dataset_id", kickoff.get("dataset_id"))
    return body, run_envelope, latency_ms


def run(case: CompanyCase) -> ProviderResult:
    enrichment = saved_enrichment()
    request_body, run_envelope, latency_ms = finished_run(case, enrichment["id"])
    dataset_id = run_envelope.get("dataset_id")
    if not isinstance(dataset_id, str) or not dataset_id:
        raise ValueError(f"Riveter run {run_envelope.get('id')} named no dataset")
    row = dataset_row(dataset_id)
    value = parse_row(row)
    credits_used = float(run_envelope.get("credits_used") or 0)
    audit = {
        "web_research_contract": request_contract(case),
        "enrichment": {
            key: enrichment.get(key) for key in ("id", "name", "tools", "budget_credits_per_row", "budget_locked", "version")
        },
        "request": {"method": "POST", "url": f"{API_BASE}/enrich", "body": request_body},
        "run": {
            key: run_envelope.get(key)
            for key in ("id", "status", "dataset_id", "credits_used", "credit_detail", "app_url", "started_at", "finished_at")
        },
        "latency_ms_definition": "kickoff request sent to terminal run status, polling included",
        "pricing": {"usd_per_credit": USD_PER_CREDIT, "checked": "2026-11-04", "url": PRICING_URL},
        "sources": value["sources"],
        "raw_response": {"run": run_envelope, "row": row},
    }
    found = has_scored_value(value)
    return ProviderResult(
        VENDOR_SLUG,
        VENDOR_NAME,
        case.case_slug,
        "ok" if found else "not_found",
        latency_ms,
        normalized=normalized_company(value) if found else None,
        cost_units=credits_used,
        cost_unit="riveter_credit",
        audit=audit,
    )
