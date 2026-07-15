from __future__ import annotations

import os
from typing import Any

from firmographic.common import CompanyCase, NormalizedCompany, ProviderResult

from .base import as_list, find_first_key, first_value, nested, parse_employee_range, request_json


VENDOR_SLUG = "fiber"
VENDOR_NAME = "Fiber"
REQUIRED_ENV = ("FIBER_API_KEY",)
PAID_CALLS_PER_CASE = 1


def preflight() -> dict[str, Any]:
    body, latency_ms = request_json(
        "GET",
        "https://api.fiber.ai/v1/get-org-credits",
        params={"apiKey": os.environ["FIBER_API_KEY"]},
    )
    return {
        "available_credits": (
            find_first_key(body, "credits")
            or find_first_key(body, "availableCredits")
            or find_first_key(body, "creditsAvailable")
            or find_first_key(body, "balance")
        ),
        "latency_ms": latency_ms,
    }


def _consensus(item: dict[str, Any], key: str) -> Any:
    value = item.get(key)
    if isinstance(value, dict):
        return value.get("value") if "value" in value else value
    return value


def run(case: CompanyCase) -> ProviderResult:
    body, latency_ms = request_json(
        "POST",
        "https://api.fiber.ai/v1/company-search",
        json_body={
            "apiKey": os.environ["FIBER_API_KEY"],
            "searchParams": {
                "exactCompanyV2": {
                    "anyOf": [{"identifier": "domain", "domain": case.input_domain}]
                }
            },
        },
    )
    records = nested(body, "output", "data", default=None)
    if records is None and isinstance(body, dict):
        records = body.get("data")
    records = records if isinstance(records, list) else ([records] if isinstance(records, dict) else [])
    charge = nested(body, "chargeInfo", "creditsCharged", default=None)
    if not records:
        return ProviderResult(
            VENDOR_SLUG, VENDOR_NAME, case.case_slug, "not_found", latency_ms,
            cost_units=charge, cost_unit="fiber_credit" if charge is not None else None,
            audit={"returned_count": 0},
        )

    item = records[0]
    name_value = first_value(item, "preferred_name") or _consensus(item, "name_consensus")
    if isinstance(name_value, dict):
        name_value = first_value(name_value, "name", "companyName", "value")
    domain_value = _consensus(item, "domain_consensus")
    if isinstance(domain_value, dict):
        domain_value = first_value(domain_value, "domain", "value")
    location = _consensus(item, "location_consensus")
    location = location if isinstance(location, dict) else {}
    headcount = (
        _consensus(item, "employee_count_consensus")
        or _consensus(item, "employee_range_consensus")
        or first_value(item, "employeeCount", "employee_count", "employeeRange", "employee_range")
    )
    low, high = parse_employee_range(headcount)
    names = as_list(first_value(item, "names", "companyNames", "company_names"))
    domains = as_list(first_value(item, "domains", "companyDomains", "company_domains"))
    linkedin = first_value(
        item,
        "linkedin_url",
        "linkedinUrl",
        "linkedin_company_url",
        "linkedinCompanyUrl",
        "linkedin_primary_slug",
        "linkedin_slug",
    )
    if linkedin and "linkedin.com" not in str(linkedin):
        linkedin = f"https://www.linkedin.com/company/{str(linkedin).strip('/')}"
    industries = as_list(
        first_value(item, "li_industries", "alt_industries", "standard_industries", "industries", "industry")
    )
    industries = [
        (v.get("name") or v.get("value")) if isinstance(v, dict) else v
        for v in industries
    ]

    company = NormalizedCompany(
        legal_name=name_value or first_value(item, "name", "companyName", "company_name") or (names[0] if names else None),
        primary_domain=domain_value or first_value(item, "domain", "website") or (domains[0] if domains else None),
        aliases=[str(v) for v in names if v],
        domains=[str(v) for v in domains if v],
        hq_country=first_value(location, "country", "country_name", "countryName") or first_value(item, "country", "countryName"),
        hq_city=first_value(location, "city") or item.get("city"),
        founded_year=first_value(
            item, "founded_on_consensus", "founded_year", "foundedYear", "yearFounded", "founded"
        ),
        industry=str(industries[0]) if industries else None,
        industries=[str(v) for v in industries if v],
        linkedin_url=linkedin,
        headcount_min=low,
        headcount_max=high,
        funding_stage=first_value(item, "funding_stage", "fundingStage", "last_funding_stage", "lastFundingStage"),
    )
    return ProviderResult(
        VENDOR_SLUG,
        VENDOR_NAME,
        case.case_slug,
        "ok",
        latency_ms,
        normalized=company,
        cost_units=charge,
        cost_unit="fiber_credit" if charge is not None else None,
        ambiguity_count=max(0, len(records) - 1),
        audit={
            "returned_count": len(records),
            "selected_index": 0,
            "selected_record_keys": sorted(item.keys()),
        },
    )
