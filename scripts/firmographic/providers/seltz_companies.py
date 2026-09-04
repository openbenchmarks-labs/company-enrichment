"""Seltz Answer companies-scope firmographic adapter."""
from __future__ import annotations

import json
import os
from typing import Any

from firmographic.common import CompanyCase, ProviderResult
from firmographic.web_research import (
    EXA_OUTPUT_SCHEMA,
    has_scored_value,
    instruction,
    normalized_company,
    parse_structured_value,
    request_contract,
)

from .base import request_json


VENDOR_SLUG = "seltz-companies"
VENDOR_NAME = "Seltz"
SCOPE = "companies"
ENDPOINT = "https://api.seltz.ai/v1/answer"
REQUIRED_ENV = ("SELTZ_API_KEY",)
PAID_CALLS_PER_CASE = 1
SYSTEM_PROMPT = (
    "Return only the requested JSON object. Use null for unknown scalar values "
    "and [] for unknown lists. Do not add prose or Markdown fences."
)
RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "company_enrichment",
        "strict": True,
        "schema": EXA_OUTPUT_SCHEMA,
    },
}


def payload(case: CompanyCase) -> dict[str, Any]:
    return {
        "query": instruction(case.input_name, case.input_domain),
        "scope": SCOPE,
        "system_prompt": SYSTEM_PROMPT,
        "response_format": RESPONSE_FORMAT,
    }


def parse_answer(value: Any, citations: Any) -> dict[str, Any]:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Seltz Answer returned no answer text")
    text = value.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else text
        if text.endswith("```"):
            text = text[: text.rindex("```")]
        text = text.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError("Seltz Answer was not valid structured JSON") from error
    sources = [
        row["url"]
        for row in citations or []
        if isinstance(row, dict) and isinstance(row.get("url"), str) and row["url"]
    ]
    return parse_structured_value({**parsed, "sources": list(dict.fromkeys(sources))})


def run(case: CompanyCase) -> ProviderResult:
    request_body = payload(case)
    body, latency_ms = request_json(
        "POST",
        ENDPOINT,
        headers={
            "x-api-key": os.environ["SELTZ_API_KEY"],
            "Content-Type": "application/json",
        },
        json_body=request_body,
        timeout=180,
    )
    if not isinstance(body, dict):
        raise ValueError("Seltz Answer returned a non-object response")
    value = parse_answer(body.get("answer"), body.get("citations"))
    audit = {
        "web_research_contract": request_contract(case),
        "request": {"method": "POST", "url": ENDPOINT, "body": request_body},
        "scope": SCOPE,
        "response_format_mode": "json_schema",
        "sources": value["sources"],
        "raw_response": body,
    }
    if not has_scored_value(value):
        return ProviderResult(
            VENDOR_SLUG,
            VENDOR_NAME,
            case.case_slug,
            "not_found",
            latency_ms,
            cost_units=1,
            cost_unit="seltz_answer_request",
            audit=audit,
        )
    return ProviderResult(
        VENDOR_SLUG,
        VENDOR_NAME,
        case.case_slug,
        "ok",
        latency_ms,
        normalized=normalized_company(value),
        cost_units=1,
        cost_unit="seltz_answer_request",
        audit=audit,
    )
