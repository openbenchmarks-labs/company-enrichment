from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote

from firmographic.common import CompanyCase, NormalizedCompany, ProviderResult

from .base import ProviderHTTPError, as_list, first_dict, request_json


VENDOR_SLUG = "predictleads"
VENDOR_NAME = "PredictLeads"
REQUIRED_ENV = ("PREDICT_LEADS_API_KEY", "PREDICT_LEADS_API_TOKEN")
PAID_CALLS_PER_CASE = 1


def _location(attributes: dict[str, Any]) -> tuple[str | None, str | None]:
    locations = [v for v in as_list(attributes.get("location_data")) if isinstance(v, dict)]
    chosen = next((v for v in locations if v.get("category") == "headquarters"), None)
    if not chosen:
        chosen = first_dict(locations)
    if chosen:
        return chosen.get("country"), chosen.get("city")
    linkedin = first_dict(attributes.get("linkedin_location_data"))
    return linkedin.get("country"), linkedin.get("city")


def run(case: CompanyCase) -> ProviderResult:
    try:
        body, latency_ms = request_json(
            "GET",
            f"https://predictleads.com/api/v3/extended_companies/{quote(case.input_domain)}",
            headers={
                "X-Api-Key": os.environ["PREDICT_LEADS_API_KEY"],
                "X-Api-Token": os.environ["PREDICT_LEADS_API_TOKEN"],
                "Accept": "application/json",
            },
        )
    except ProviderHTTPError as exc:
        if exc.status_code == 404:
            return ProviderResult(
                VENDOR_SLUG,
                VENDOR_NAME,
                case.case_slug,
                "not_found",
                exc.elapsed_ms or 0,
                cost_units=1,
                cost_unit="extended_company_request",
                audit={"vendor_status_code": exc.status_code, "retry_after": exc.retry_after},
            )
        raise
    records = body.get("data") if isinstance(body, dict) else None
    records = records if isinstance(records, list) else ([records] if isinstance(records, dict) else [])
    if not records:
        return ProviderResult(VENDOR_SLUG, VENDOR_NAME, case.case_slug, "not_found", latency_ms)

    record = records[0]
    attributes = record.get("attributes") or {}
    relationships = record.get("relationships") or {}
    redirect = (relationships.get("redirects_to") or {}).get("data")
    redirect_reason = ((relationships.get("redirects_to") or {}).get("meta") or {}).get("reason")
    included = body.get("included") or []
    target = next((v for v in included if redirect and v.get("id") == redirect.get("id")), None)
    target_attributes = (target or {}).get("attributes") or {}
    country, city = _location(attributes)

    aliases = [
        attributes.get("company_name"),
        attributes.get("friendly_company_name"),
    ]
    if target_attributes.get("company_name"):
        aliases.append(target_attributes["company_name"])

    company = NormalizedCompany(
        legal_name=target_attributes.get("company_name") or attributes.get("company_name") or attributes.get("friendly_company_name"),
        primary_domain=target_attributes.get("domain") or attributes.get("domain"),
        aliases=[v for v in aliases if v],
        domains=[v for v in [attributes.get("domain"), target_attributes.get("domain")] if v],
        hq_country=country,
        hq_city=city,
        founded_year=attributes.get("linkedin_founded"),
        industry=attributes.get("linkedin_industry"),
        industries=[attributes.get("linkedin_industry")] if attributes.get("linkedin_industry") else [],
        linkedin_url=attributes.get("linkedin_url"),
        headcount_min=attributes.get("employees_min"),
        headcount_max=attributes.get("employees_max"),
        funding_stage=attributes.get("linkedin_funding_last_round_stage"),
    )
    return ProviderResult(
        VENDOR_SLUG,
        VENDOR_NAME,
        case.case_slug,
        "ok",
        latency_ms,
        normalized=company,
        cost_units=1,
        cost_unit="extended_company_request",
        ambiguity_count=max(0, len(records) - 1),
        audit={
            "selected_record_id": record.get("id"),
            "returned_count": len(records),
            "schema_version": (body.get("meta") or {}).get("schema_version"),
            "redirect_reason": redirect_reason,
            "redirect_applied": bool(target),
        },
    )
