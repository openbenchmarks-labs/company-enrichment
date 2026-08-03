"""Parallel Responses adapter using the shared web-research enrichment contract."""

from __future__ import annotations

import os
from typing import Any

from firmographic.common import CompanyCase, ProviderResult
from firmographic.web_research import (
    OUTPUT_SCHEMA,
    has_scored_value,
    instruction,
    normalized_company,
    parse_response,
    request_contract,
)

from .base import request_json


VENDOR_SLUG = "parallel-research"
VENDOR_NAME = "Parallel"
REQUIRED_ENV = ("PARALLEL_API_KEY",)
PAID_CALLS_PER_CASE = 1


def run(case: CompanyCase) -> ProviderResult:
    body, latency_ms = request_json(
        "POST",
        "https://api.parallel.ai/v1/responses",
        headers={
            "Authorization": f"Bearer {os.environ['PARALLEL_API_KEY']}",
            "Content-Type": "application/json",
        },
        json_body={
            "model": "parallel",
            "input": instruction(case.input_name, case.input_domain),
            "reasoning": {"effort": "medium"},
            "text": {"format": {"type": "json_schema", "name": "company_enrichment", "schema": OUTPUT_SCHEMA}},
        },
        timeout=180,
    )
    if not isinstance(body, dict):
        raise ValueError("Parallel returned a non-object response")
    value = parse_response(body)
    audit: dict[str, Any] = {
        "web_research_contract": request_contract(case),
        "response_id": body.get("id"),
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
            cost_unit="parallel_responses_medium_request",
            audit=audit,
        )
    return ProviderResult(
        VENDOR_SLUG, VENDOR_NAME, case.case_slug, "ok", latency_ms,
        normalized=normalized_company(value),
        cost_units=1,
        cost_unit="parallel_responses_medium_request",
        audit=audit,
    )
