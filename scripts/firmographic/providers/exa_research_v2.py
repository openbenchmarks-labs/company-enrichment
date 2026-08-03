"""Exa company-enrichment adapter using the documented search request shape."""

from __future__ import annotations

import os

from firmographic.common import CompanyCase, ProviderResult
from firmographic.web_research import (
    EXA_V2_OUTPUT_SCHEMA,
    exa_v2_query,
    exa_v2_system_prompt,
    has_scored_value,
    normalized_company,
    parse_exa_v2_response,
    request_contract,
)

from .base import request_json
from .exa_research import _await_request_slot


VENDOR_SLUG = "exa-research-v2"
VENDOR_NAME = "Exa (documented enrichment request)"
REQUIRED_ENV = ("EXA_API_KEY",)
PAID_CALLS_PER_CASE = 1


def run(case: CompanyCase) -> ProviderResult:
    _await_request_slot()
    body, latency_ms = request_json(
        "POST", "https://api.exa.ai/search",
        headers={"x-api-key": os.environ["EXA_API_KEY"], "Content-Type": "application/json"},
        json_body={
            "query": exa_v2_query(case.input_name, case.input_domain),
            "type": "deep-reasoning",
            "category": "company",
            "numResults": 10,
            "additionalQueries": [
                f"site:{case.input_domain}",
                f'site:linkedin.com/company "{case.input_name or case.input_domain}"',
            ],
            "contents": {"highlights": {"query": "official company name, website, headquarters, founding year, LinkedIn company page, employee count, industry", "maxCharacters": 6000}},
            "systemPrompt": exa_v2_system_prompt(case.input_name, case.input_domain),
            "outputSchema": EXA_V2_OUTPUT_SCHEMA,
        },
        timeout=180,
    )
    if not isinstance(body, dict):
        raise ValueError("Exa returned a non-object response")
    value = parse_exa_v2_response(body, input_domain=case.input_domain)
    audit = {
        "web_research_contract": request_contract(case),
        "request_version": "exa-company-enrichment-v2",
        "response_id": body.get("requestId"),
        "sources": value["sources"],
        "raw_response": body,
    }
    if not has_scored_value(value):
        return ProviderResult(VENDOR_SLUG, VENDOR_NAME, case.case_slug, "not_found", latency_ms, audit=audit)
    return ProviderResult(VENDOR_SLUG, VENDOR_NAME, case.case_slug, "ok", latency_ms, normalized=normalized_company(value), cost_units=1, cost_unit="exa_deep_reasoning_request", audit=audit)
