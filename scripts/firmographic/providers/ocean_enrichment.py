from __future__ import annotations

import os
from typing import Any

from firmographic.common import CompanyCase, NormalizedCompany, ProviderResult

from .base import ProviderHTTPError, as_list, parse_employee_range, request_json


VENDOR_SLUG = "ocean"
VENDOR_NAME = "Ocean.io"
REQUIRED_ENV = ("OCEAN_API_KEY",)
PAID_CALLS_PER_CASE = 1

FIELDS = [
    "domain",
    "primaryCountry",
    "companySize",
    "industries",
    "industryCategories",
    "linkedinIndustry",
    "employeeCountOcean",
    "employeeCountLinkedin",
    "yearFounded",
    "medias.linkedin",
    "name",
    "legalName",
    "locations",
    "fundingRound",
    "rootUrl",
]


def _primary_location(value: Any) -> dict[str, Any]:
    locations = [item for item in as_list(value) if isinstance(item, dict)]
    return next((item for item in locations if item.get("primary")), locations[0] if locations else {})


def _linkedin(value: Any) -> str | None:
    if isinstance(value, str):
        if "linkedin.com" not in value:
            return f"https://www.linkedin.com/company/{value.strip('/')}"
        return value
    if isinstance(value, dict):
        raw = value.get("url") or value.get("handle") or value.get("name")
        if raw and "linkedin.com" not in str(raw):
            return f"https://www.linkedin.com/company/{str(raw).strip('/')}"
        return raw
    return None


def run(case: CompanyCase) -> ProviderResult:
    try:
        body, latency_ms = request_json(
            "POST",
            "https://api.ocean.io/v2/enrich/company",
            headers={
                "X-Api-Token": os.environ["OCEAN_API_KEY"],
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json_body={"company": {"domain": case.input_domain}, "fields": FIELDS},
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
    if not isinstance(body, dict):
        return ProviderResult(VENDOR_SLUG, VENDOR_NAME, case.case_slug, "not_found", latency_ms)
    if body.get("detail") and not body.get("domain"):
        return ProviderResult(
            VENDOR_SLUG,
            VENDOR_NAME,
            case.case_slug,
            "pending",
            latency_ms,
            audit={"detail": body.get("detail")},
        )
    if not body.get("domain"):
        return ProviderResult(VENDOR_SLUG, VENDOR_NAME, case.case_slug, "not_found", latency_ms)

    location = _primary_location(body.get("locations"))
    medias = body.get("medias") if isinstance(body.get("medias"), dict) else {}
    funding = body.get("fundingRound") if isinstance(body.get("fundingRound"), dict) else {}
    low, high = parse_employee_range(body.get("companySize"))
    if low is None and high is None:
        low, high = parse_employee_range(
            body.get("employeeCountLinkedin") or body.get("employeeCountOcean")
        )
    industries = [str(value) for value in as_list(body.get("industries")) if value]
    if body.get("linkedinIndustry") and body["linkedinIndustry"] not in industries:
        industries.insert(0, body["linkedinIndustry"])
    credits = body.get("creditsUsed")
    company = NormalizedCompany(
        legal_name=body.get("legalName") or body.get("name"),
        primary_domain=body.get("domain") or body.get("rootUrl"),
        hq_country=body.get("primaryCountry") or location.get("country"),
        hq_city=location.get("locality") or location.get("city"),
        founded_year=body.get("yearFounded"),
        industry=industries[0] if industries else None,
        industries=industries,
        linkedin_url=_linkedin(medias.get("linkedin")),
        headcount_min=low,
        headcount_max=high,
        funding_stage=funding.get("type"),
    )
    return ProviderResult(
        VENDOR_SLUG,
        VENDOR_NAME,
        case.case_slug,
        "ok",
        latency_ms,
        normalized=company,
        cost_units=float(credits) if isinstance(credits, (int, float)) else None,
        cost_unit="ocean_credit" if isinstance(credits, (int, float)) else None,
        audit={
            "reported_employee_count_ocean": body.get("employeeCountOcean"),
            "reported_employee_count_linkedin": body.get("employeeCountLinkedin"),
            "response_keys": sorted(body.keys()),
        },
    )
