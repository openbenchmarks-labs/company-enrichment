from __future__ import annotations

import os
from typing import Any

from firmographic.common import CompanyCase, NormalizedCompany, ProviderResult

from .base import as_list, find_first_key, first_value, parse_employee_range, request_json


VENDOR_SLUG = "explorium"
VENDOR_NAME = "Explorium"
REQUIRED_ENV = ("EXPLORIUM_API_KEY",)
PAID_CALLS_PER_CASE = 2


def _records(body: Any) -> list[dict[str, Any]]:
    if isinstance(body, list):
        return [v for v in body if isinstance(v, dict)]
    if not isinstance(body, dict):
        return []
    for key in ("data", "businesses", "results", "matched_businesses"):
        value = body.get(key)
        if isinstance(value, list):
            return [v for v in value if isinstance(v, dict)]
    return []


def run(case: CompanyCase) -> ProviderResult:
    headers = {"api_key": os.environ["EXPLORIUM_API_KEY"], "Content-Type": "application/json"}
    match_body, match_ms = request_json(
        "POST",
        "https://api.explorium.ai/v1/businesses/match",
        headers=headers,
        json_body={
            "businesses_to_match": [
                {"name": case.input_name, "domain": case.input_domain}
            ]
        },
    )
    business_id = find_first_key(match_body, "business_id") or find_first_key(match_body, "businessId")
    if not business_id:
        return ProviderResult(
            VENDOR_SLUG, VENDOR_NAME, case.case_slug, "not_found", match_ms,
            cost_units=1, cost_unit="api_request",
            audit={"match_count": len(_records(match_body))},
        )

    fetch_body, fetch_ms = request_json(
        "POST",
        "https://api.explorium.ai/v1/businesses",
        headers=headers,
        json_body={
            "filters": {"business_id": {"values": [business_id]}},
            "mode": "full",
            "page_size": 10,
            "size": 10,
            "page": 1,
        },
    )
    records = _records(fetch_body)
    selected = next(
        (v for v in records if str(v.get("business_id") or v.get("businessId")) == str(business_id)),
        records[0] if records else None,
    )
    if not selected:
        return ProviderResult(
            VENDOR_SLUG, VENDOR_NAME, case.case_slug, "not_found", match_ms + fetch_ms,
            cost_units=2, cost_unit="api_request",
            audit={"matched_business_id": str(business_id), "fetch_count": 0},
        )

    headcount = first_value(
        selected,
        "number_of_employees_range",
        "employee_count_range",
        "employees_range",
        "number_of_employees",
    )
    low, high = parse_employee_range(headcount)
    industries = as_list(
        first_value(selected, "industry", "industries", "naics_description", "sic_description")
    )
    linkedin = first_value(
        selected, "linkedin", "linkedin_url", "linkedin_profile", "linkedin_company_url"
    )
    if isinstance(linkedin, dict):
        linkedin = first_value(linkedin, "url", "profile_url", "value")
    company = NormalizedCompany(
        legal_name=first_value(selected, "company_name", "business_name", "name", "legal_name"),
        primary_domain=first_value(selected, "domain", "website", "website_domain"),
        aliases=[str(v) for v in as_list(first_value(selected, "aliases", "company_names")) if v],
        domains=[str(v) for v in as_list(first_value(selected, "domains", "websites")) if v],
        hq_country=first_value(selected, "country", "country_name", "hq_country"),
        hq_city=first_value(selected, "city_name", "city", "hq_city"),
        founded_year=first_value(selected, "founded_year", "year_founded", "founded"),
        industry=str(industries[0]) if industries else None,
        industries=[str(v) for v in industries if v],
        linkedin_url=linkedin,
        headcount_min=low,
        headcount_max=high,
        funding_stage=first_value(selected, "funding_stage", "last_funding_stage"),
    )
    return ProviderResult(
        VENDOR_SLUG,
        VENDOR_NAME,
        case.case_slug,
        "ok",
        match_ms + fetch_ms,
        normalized=company,
        cost_units=2,
        cost_unit="api_request",
        ambiguity_count=max(0, len(records) - 1),
        audit={
            "matched_business_id": str(business_id),
            "fetch_count": len(records),
            "selected_record_keys": sorted(selected.keys()),
            "match_latency_ms": match_ms,
            "fetch_latency_ms": fetch_ms,
        },
    )
