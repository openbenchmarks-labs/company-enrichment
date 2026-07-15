from __future__ import annotations

import os
from typing import Any

from firmographic.common import CompanyCase, NormalizedCompany, ProviderResult

from .base import ProviderHTTPError, as_list, request_json


VENDOR_SLUG = "company-enrich"
VENDOR_NAME = "CompanyEnrich"
REQUIRED_ENV = ("COMPANY_ENRICH_API_KEY",)
PAID_CALLS_PER_CASE = 1

EMPLOYEE_RANGES: dict[str, tuple[int | None, int | None]] = {
    "1-10": (1, 10),
    "11-50": (11, 50),
    "51-200": (51, 200),
    "201-500": (201, 500),
    "501-1K": (501, 1_000),
    "1K-5K": (1_001, 5_000),
    "5K-10K": (5_001, 10_000),
    "over-10K": (10_001, None),
}


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def run(case: CompanyCase) -> ProviderResult:
    try:
        body, latency_ms = request_json(
            "GET",
            "https://api.companyenrich.com/companies/enrich",
            headers={
                "Authorization": f"Bearer {os.environ['COMPANY_ENRICH_API_KEY']}",
                "Accept": "application/json",
            },
            params={"domain": case.input_domain},
        )
    except ProviderHTTPError as exc:
        if exc.status_code == 404:
            return ProviderResult(
                VENDOR_SLUG,
                VENDOR_NAME,
                case.case_slug,
                "not_found",
                exc.elapsed_ms or 0,
                cost_units=1.0,
                cost_unit="company_enrich_credit",
                audit={"vendor_status_code": exc.status_code, "retry_after": exc.retry_after},
            )
        raise
    if not isinstance(body, dict) or not body.get("id"):
        return ProviderResult(
            VENDOR_SLUG,
            VENDOR_NAME,
            case.case_slug,
            "not_found",
            latency_ms,
            cost_units=1.0,
            cost_unit="company_enrich_credit",
        )

    location = _object(body.get("location"))
    country = _object(location.get("country"))
    city = _object(location.get("city"))
    socials = _object(body.get("socials"))
    financial = _object(body.get("financial"))
    low, high = EMPLOYEE_RANGES.get(str(body.get("employees")), (None, None))
    industries = [str(value) for value in as_list(body.get("industries")) if value]
    if body.get("industry") and body["industry"] not in industries:
        industries.insert(0, str(body["industry"]))
    company = NormalizedCompany(
        legal_name=body.get("name"),
        primary_domain=body.get("domain") or body.get("website"),
        hq_country=country.get("name") or country.get("code"),
        hq_city=city.get("name"),
        founded_year=body.get("founded_year"),
        industry=industries[0] if industries else None,
        industries=industries,
        linkedin_url=socials.get("linkedin_url"),
        headcount_min=low,
        headcount_max=high,
        funding_stage=financial.get("funding_stage"),
    )
    return ProviderResult(
        VENDOR_SLUG,
        VENDOR_NAME,
        case.case_slug,
        "ok",
        latency_ms,
        normalized=company,
        cost_units=1.0,
        cost_unit="company_enrich_credit",
        audit={
            "record_id": body.get("id"),
            "updated_at": body.get("updated_at"),
            "response_keys": sorted(body.keys()),
        },
    )
