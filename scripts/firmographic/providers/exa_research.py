"""Exa deep-reasoning adapter using the shared web-research enrichment contract."""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from firmographic.common import CompanyCase, ProviderResult
from firmographic.web_research import (
    EXA_OUTPUT_SCHEMA,
    has_scored_value,
    instruction,
    normalized_company,
    parse_exa_response,
    request_contract,
)

from .base import request_json


VENDOR_SLUG = "exa-research"
VENDOR_NAME = "Exa"
REQUIRED_ENV = ("EXA_API_KEY",)
PAID_CALLS_PER_CASE = 1

# Exa accounts enforce a 10 request/second ceiling. Limit starts globally rather
# than per worker so a concurrent benchmark cannot create a burst of 429s.
_RATE_LOCK = threading.Lock()
_NEXT_REQUEST_AT = 0.0
_MIN_SECONDS_BETWEEN_REQUESTS = 0.2  # five starts/sec; leaves headroom below 10.


def _await_request_slot() -> None:
    global _NEXT_REQUEST_AT
    with _RATE_LOCK:
        now = time.monotonic()
        slot = max(now, _NEXT_REQUEST_AT)
        _NEXT_REQUEST_AT = slot + _MIN_SECONDS_BETWEEN_REQUESTS
    if slot > now:
        time.sleep(slot - now)


def run(case: CompanyCase) -> ProviderResult:
    _await_request_slot()
    body, latency_ms = request_json(
        "POST",
        "https://api.exa.ai/search",
        headers={"x-api-key": os.environ["EXA_API_KEY"], "Content-Type": "application/json"},
        json_body={
            "query": instruction(case.input_name, case.input_domain),
            "type": "deep-reasoning",
            "output_schema": EXA_OUTPUT_SCHEMA,
        },
        timeout=180,
    )
    if not isinstance(body, dict):
        raise ValueError("Exa returned a non-object response")
    value = parse_exa_response(body)
    audit: dict[str, Any] = {
        "web_research_contract": request_contract(case),
        "response_id": body.get("id"),
        "sources": value["sources"],
        "raw_response": body,
    }
    if not has_scored_value(value):
        return ProviderResult(VENDOR_SLUG, VENDOR_NAME, case.case_slug, "not_found", latency_ms, audit=audit)
    return ProviderResult(
        VENDOR_SLUG, VENDOR_NAME, case.case_slug, "ok", latency_ms,
        normalized=normalized_company(value),
        cost_unit="exa_research_response",
        audit=audit,
    )
