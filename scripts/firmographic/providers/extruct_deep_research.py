"""Extruct Deep Research adapter. Same instruction as Parallel/Exa; not AI Tables."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from firmographic.common import CompanyCase, NormalizedCompany, ProviderResult
from firmographic.web_research import has_scored_value, instruction, request_contract


VENDOR_SLUG = "extruct-deep-research"
VENDOR_NAME = "Extruct Deep Research"
REQUIRED_ENV = ("EXTRUCT_API_TOKEN",)
PAID_CALLS_PER_CASE = 1
API = "https://api.extruct.ai/v1/deep_research_tasks"
DEPTH = os.environ.get("EXTRUCT_DR_DEPTH", "high")
# Extruct rejects ["string","null"] unions. Omit unknown fields instead of null.
OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "legal_name": {"type": "string", "description": "Official company or legal name."},
        "primary_domain": {
            "type": "string",
            "description": "Canonical public website domain only, without a URL path.",
        },
        "hq_country": {"type": "string", "description": "Country of headquarters."},
        "hq_city": {"type": "string", "description": "City of headquarters."},
        "founded_year": {"type": "integer", "description": "Four-digit founding year."},
        "industry": {"type": "string", "description": "Primary industry label."},
        "industries": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Up to four relevant industry labels.",
        },
        "linkedin_url": {
            "type": "string",
            "description": "Canonical LinkedIn company or school page, not a person or post.",
        },
        "headcount_min": {
            "type": "integer",
            "description": "Lower bound of the LinkedIn company-size band, not members-on-LinkedIn.",
        },
        "headcount_max": {
            "type": "integer",
            "description": "Upper bound of the LinkedIn company-size band.",
        },
        "sources": {"type": "array", "items": {"type": "string"}},
    },
}


def _brief(case: CompanyCase) -> str:
    # Shared identity contract, plus Extruct-specific omit-unknown / LinkedIn-band notes.
    return (
        instruction(case.input_name, case.input_domain)
        + "\n\nOmit any field you cannot verify; do not guess. "
        "LinkedIn URL must be the main /company/ or /school/ page of THIS "
        "identity — never a person, post, department, library, alumni, or "
        "division page. Headcount: LinkedIn company-size BAND (for example "
        "11-50 or 51-200), not the employees-on-LinkedIn member count. "
        "Industry: copy the LinkedIn company-page industry string as industry "
        "when it exists; put adjacent labels in industries. Founded year: the "
        "brand/organization year on the website About page or LinkedIn, not a "
        "later holding-company or GmbH/Ltd incorporation. HQ: use the LinkedIn "
        "HQ locality even when it is a suburb, district, or nationwide label; "
        "do not upgrade a suburb to the nearest famous city. If the supplied "
        "website is a subdomain or section of a larger organization's site, "
        "the target IS that organization."
    )


def _request(method: str, url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    token = os.environ["EXTRUCT_API_TOKEN"].strip()
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    for attempt in range(1, 5):
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:800]
            if error.code not in {429, 500, 502, 503, 504} or attempt == 4:
                raise RuntimeError(f"HTTP {error.code}: {detail}") from error
            time.sleep(min(2 ** attempt, 60))
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == 4:
                raise
            time.sleep(min(2 ** attempt, 60))
    raise RuntimeError("unreachable")


def _unwrap(value: Any) -> Any:
    if isinstance(value, dict) and "value" in value and ("sources" in value or "reasoning" in value):
        return value.get("value")
    return value


def _text(value: Any) -> str | None:
    value = _unwrap(value)
    if value in (None, "", [], {}):
        return None
    text = str(value).strip()
    return text or None


def _int(value: Any) -> int | None:
    value = _unwrap(value)
    if value in (None, "", [], {}) or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(float(str(value).replace(",", "").replace("$", "").strip()))
    except (TypeError, ValueError):
        return None


def _list(value: Any) -> list[str]:
    value = _unwrap(value)
    if value in (None, "", []):
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _run_task(brief: str) -> dict[str, Any]:
    started = time.perf_counter()
    created = _request("POST", API, {"brief": brief, "depth": DEPTH, "output_schema": OUTPUT_SCHEMA})
    task_id = created.get("id")
    if not task_id:
        raise RuntimeError(f"create returned no id: {created!r}"[:400])
    deadline = time.time() + 1500
    latest = created
    while time.time() < deadline:
        latest = _request("GET", f"{API}/{task_id}")
        status = latest.get("status")
        if status == "done":
            report = latest.get("report") or {}
            fields = report.get("fields") if isinstance(report, dict) else None
            if not isinstance(fields, dict):
                raise RuntimeError(f"task {task_id} done without schema fields")
            agents = latest.get("agents")
            agent_count = agents if isinstance(agents, int) else len(agents or [])
            return {
                "task_id": task_id,
                "fields": fields,
                "agents": agent_count,
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "cost_units": agent_count * 2,
                "degradation_reasons": report.get("degradation_reasons") or [],
            }
        if status == "failed":
            raise RuntimeError(f"task {task_id} failed: {latest.get('failure_reason') or 'no reason'}")
        time.sleep(15)
    raise TimeoutError(f"task {task_id} timed out (status={latest.get('status')})")


def run(case: CompanyCase) -> ProviderResult:
    result = _run_task(_brief(case))
    fields = result["fields"]
    value = {
        "legal_name": _text(fields.get("legal_name")),
        "primary_domain": _text(fields.get("primary_domain")) or case.input_domain,
        "hq_country": _text(fields.get("hq_country")),
        "hq_city": _text(fields.get("hq_city")),
        "founded_year": _int(fields.get("founded_year")),
        "industry": _text(fields.get("industry")),
        "industries": _list(fields.get("industries")),
        "linkedin_url": _text(fields.get("linkedin_url")),
        "headcount_min": _int(fields.get("headcount_min")),
        "headcount_max": _int(fields.get("headcount_max")),
        "sources": _list(fields.get("sources")),
    }
    audit = {
        "web_research_contract": request_contract(case),
        "task_id": result["task_id"],
        "agents": result["agents"],
        "sources": value["sources"],
        "degradation_reasons": result["degradation_reasons"],
        "depth": DEPTH,
    }
    if not has_scored_value(value):
        return ProviderResult(
            VENDOR_SLUG,
            VENDOR_NAME,
            case.case_slug,
            "not_found",
            result["latency_ms"],
            cost_units=result["cost_units"],
            cost_unit="extruct_deep_research_credits",
            audit=audit,
        )
    return ProviderResult(
        VENDOR_SLUG,
        VENDOR_NAME,
        case.case_slug,
        "ok",
        result["latency_ms"],
        normalized=NormalizedCompany(
            legal_name=value["legal_name"],
            primary_domain=value["primary_domain"],
            hq_country=value["hq_country"],
            hq_city=value["hq_city"],
            founded_year=value["founded_year"],
            industry=value["industry"],
            industries=value["industries"],
            linkedin_url=value["linkedin_url"],
            headcount_min=value["headcount_min"],
            headcount_max=value["headcount_max"],
        ),
        cost_units=result["cost_units"],
        cost_unit="extruct_deep_research_credits",
        audit=audit,
    )
