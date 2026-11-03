from __future__ import annotations

import os
from typing import Any

from firmographic.common import CompanyCase, NormalizedCompany, ProviderResult

from .base import ProviderHTTPError, parse_employee_range, request_json


VENDOR_SLUG = "cufinder"
VENDOR_NAME = "CUFinder"
REQUIRED_ENV = ("CUFINDER_API_KEY",)
PAID_CALLS_PER_CASE = 1
COST_UNIT = "cufinder_credit"
ENDPOINT = "https://api.cufinder.io/v3/companies/enrich"


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _credits(body: Any) -> float:
    charged = _object(_object(_object(body).get("meta")).get("credits")).get("charged")
    try:
        return float(charged)
    except (TypeError, ValueError):
        return 0.0


def _not_found(case: CompanyCase, latency_ms: int, credits: float, audit: dict[str, Any]) -> ProviderResult:
    return ProviderResult(
        VENDOR_SLUG,
        VENDOR_NAME,
        case.case_slug,
        "not_found",
        latency_ms,
        cost_units=credits,
        cost_unit=COST_UNIT,
        audit=audit,
    )


def run(case: CompanyCase) -> ProviderResult:
    try:
        body, latency_ms = request_json(
            "POST",
            ENDPOINT,
            headers={
                "x-api-key": os.environ["CUFINDER_API_KEY"],
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json_body={"query": case.input_domain},
        )
    except ProviderHTTPError as exc:
        if exc.status_code in (404, 422):
            return _not_found(
                case,
                exc.elapsed_ms or 0,
                0.0,
                {"vendor_status_code": exc.status_code, "retry_after": exc.retry_after},
            )
        raise

    company = _object(_object(_object(body).get("data")).get("company"))
    if not company or not (company.get("name") or company.get("domain")):
        return _not_found(case, latency_ms, _credits(body), {"success": _object(body).get("success")})

    location = _object(company.get("location"))
    industry = _object(company.get("industry"))
    social = _object(company.get("social"))
    size = _object(company.get("size"))
    funding = _object(company.get("funding"))

    # Prefer the published employee band; fall back to the exact count.
    low, high = parse_employee_range(size.get("employee_range"))
    exact = size.get("employee_count")
    if low is None and high is None and isinstance(exact, (int, float)) and exact > 0:
        low = high = int(exact)

    industry_name = industry.get("name") if industry else None
    normalized = NormalizedCompany(
        legal_name=company.get("name"),
        primary_domain=company.get("domain") or company.get("website_url"),
        hq_country=location.get("country_code") or location.get("country"),
        hq_city=location.get("city"),
        founded_year=company.get("founded_year"),
        industry=industry_name,
        industries=[industry_name] if industry_name else [],
        linkedin_url=social.get("linkedin"),
        headcount_min=low,
        headcount_max=high,
        funding_stage=funding.get("round_type"),
    )
    meta = _object(_object(body).get("meta"))
    return ProviderResult(
        VENDOR_SLUG,
        VENDOR_NAME,
        case.case_slug,
        "ok",
        latency_ms,
        normalized=normalized,
        cost_units=_credits(body),
        cost_unit=COST_UNIT,
        audit={
            "confidence": meta.get("confidence"),
            "linkedin_id": company.get("linkedin_id"),
            "employee_range": size.get("employee_range"),
            "employee_count": size.get("employee_count"),
            "response_keys": sorted(company.keys()),
        },
    )
