from __future__ import annotations

import os
from typing import Any

from firmographic.common import CompanyCase, NormalizedCompany, ProviderResult

from .base import as_list, first_value, parse_employee_range, request_json


VENDOR_SLUG = "apollo"
VENDOR_NAME = "Apollo"
REQUIRED_ENV = ("APOLLO_API_KEY",)
PAID_CALLS_PER_CASE = 2


def preflight() -> dict[str, Any]:
    body, latency_ms = request_json(
        "GET",
        "https://api.apollo.io/v1/auth/health",
        headers={"x-api-key": os.environ["APOLLO_API_KEY"]},
    )
    return {"authenticated": bool(body), "latency_ms": latency_ms}


def run(case: CompanyCase) -> ProviderResult:
    headers = {"x-api-key": os.environ["APOLLO_API_KEY"], "Content-Type": "application/json"}
    search_body, search_ms = request_json(
        "POST",
        "https://api.apollo.io/api/v1/mixed_companies/search",
        headers=headers,
        json_body={
            "q_organization_domains_list": [case.input_domain],
            "page": 1,
            "per_page": 1,
        },
    )
    organizations = search_body.get("organizations") if isinstance(search_body, dict) else None
    organizations = organizations if isinstance(organizations, list) else []
    if not organizations:
        return ProviderResult(
            VENDOR_SLUG, VENDOR_NAME, case.case_slug, "not_found", search_ms,
            cost_units=1, cost_unit="credit_event",
            audit={"search_count": 0},
        )
    organization_id = organizations[0].get("id")
    if not organization_id:
        return ProviderResult(
            VENDOR_SLUG, VENDOR_NAME, case.case_slug, "error", search_ms,
            cost_units=1, cost_unit="credit_event", error="search result had no organization id",
        )

    detail_body, detail_ms = request_json(
        "GET",
        f"https://api.apollo.io/api/v1/organizations/{organization_id}",
        headers=headers,
    )
    organization = detail_body.get("organization") if isinstance(detail_body, dict) else None
    if not isinstance(organization, dict):
        organization = detail_body if isinstance(detail_body, dict) else {}
    headcount = first_value(organization, "estimated_num_employees", "employee_count", "employees")
    low, high = parse_employee_range(headcount)
    if low == high and low is not None:
        low, high = low, low
    industries = as_list(first_value(organization, "industries", "industry"))
    linkedin = first_value(organization, "linkedin_url", "linkedin_company_url")
    company = NormalizedCompany(
        legal_name=first_value(organization, "name", "organization_name", "legal_name"),
        primary_domain=first_value(organization, "primary_domain", "website_url", "domain"),
        aliases=[str(v) for v in as_list(first_value(organization, "names", "aliases")) if v],
        domains=[str(v) for v in as_list(first_value(organization, "domains", "website_url")) if v],
        hq_country=first_value(organization, "country", "country_name"),
        hq_city=first_value(organization, "city"),
        founded_year=first_value(organization, "founded_year", "founded"),
        industry=str(industries[0]) if industries else None,
        industries=[str(v) for v in industries if v],
        linkedin_url=linkedin,
        headcount_min=low,
        headcount_max=high,
        funding_stage=first_value(organization, "latest_funding_stage", "funding_stage"),
    )
    return ProviderResult(
        VENDOR_SLUG,
        VENDOR_NAME,
        case.case_slug,
        "ok",
        search_ms + detail_ms,
        normalized=company,
        cost_units=2,
        cost_unit="credit_event",
        ambiguity_count=max(0, len(organizations) - 1),
        audit={
            "organization_id": organization_id,
            "search_count": len(organizations),
            "search_latency_ms": search_ms,
            "detail_latency_ms": detail_ms,
            "selected_record_keys": sorted(organization.keys()),
        },
    )
