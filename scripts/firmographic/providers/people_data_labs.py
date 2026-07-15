from __future__ import annotations

import os
from typing import Any

from firmographic.common import CompanyCase, NormalizedCompany, ProviderResult

from .base import ProviderHTTPError, as_list, parse_employee_range, request_json_with_headers


VENDOR_SLUG = "people-data-labs"
VENDOR_NAME = "People Data Labs"
REQUIRED_ENV = ("PEOPLE_DATA_LABS_API_KEY",)
PAID_CALLS_PER_CASE = 1


def _location(value: Any) -> tuple[str | None, str | None]:
    if not isinstance(value, dict):
        return None, None
    return (
        value.get("country") or value.get("country_name"),
        value.get("locality") or value.get("city") or value.get("name"),
    )

def run(case: CompanyCase) -> ProviderResult:
    try:
        body, latency_ms, headers = request_json_with_headers(
            "GET",
            "https://api.peopledatalabs.com/v5/company/enrich",
            headers={
                "X-Api-Key": os.environ["PEOPLE_DATA_LABS_API_KEY"],
                "Accept": "application/json",
            },
            params={"website": case.input_domain},
        )
    except ProviderHTTPError as exc:
        if exc.status_code == 404:
            return ProviderResult(
                VENDOR_SLUG,
                VENDOR_NAME,
                case.case_slug,
                "not_found",
                exc.elapsed_ms or 0,
                audit={"vendor_status_code": exc.status_code, "retry_after": exc.retry_after},
            )
        raise
    if not isinstance(body, dict) or not body.get("id"):
        return ProviderResult(VENDOR_SLUG, VENDOR_NAME, case.case_slug, "not_found", latency_ms)

    country, city = _location(body.get("location"))
    low, high = parse_employee_range(body.get("size"))
    if low is None and high is None:
        low, high = parse_employee_range(body.get("employee_count"))
    aliases = [value for value in as_list(body.get("alternative_names")) if isinstance(value, str)]
    domains = [value for value in as_list(body.get("alternative_domains")) if isinstance(value, str)]
    industries = [body.get("industry")] if body.get("industry") else []
    credits_raw = headers.get("X-Call-Credits-Spent") or headers.get("x-call-credits-spent")
    try:
        credits = float(credits_raw) if credits_raw is not None else None
    except ValueError:
        credits = None
    company = NormalizedCompany(
        legal_name=body.get("legal_name") or body.get("display_name") or body.get("name"),
        primary_domain=body.get("website") or body.get("website_url"),
        aliases=aliases,
        domains=domains,
        hq_country=country,
        hq_city=city,
        founded_year=body.get("founded"),
        industry=body.get("industry"),
        industries=industries,
        linkedin_url=body.get("linkedin_url"),
        headcount_min=low,
        headcount_max=high,
        funding_stage=body.get("latest_funding_stage") or body.get("funding_stage"),
    )
    return ProviderResult(
        VENDOR_SLUG,
        VENDOR_NAME,
        case.case_slug,
        "ok",
        latency_ms,
        normalized=company,
        cost_units=credits,
        cost_unit="pdl_company_enrichment_credit" if credits is not None else None,
        audit={
            "record_id": body.get("id"),
            "dataset_version": body.get("dataset_version"),
            "likelihood": body.get("likelihood"),
            "reported_employee_count": body.get("employee_count"),
            "response_keys": sorted(body.keys()),
        },
    )
