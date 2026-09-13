"""OpenBenchmarks company-enrichment adapter for the Pvalyou company API.

Drop-in for openbenchmarks-labs/company-enrichment: copy to
`scripts/firmographic/providers/pvalyou.py`, set PVALYOU_API_KEY, and their runner
(`run_firmographic_full_benchmark.py`) calls `run(case)` per company like every other provider.

What it calls: POST {PVALYOU_API_BASE}/company with the benchmark's domain and name. Pvalyou reads
the company live (its website, its LinkedIn page, public registries and third-party pages), so a
company that is not on file answers as a job: the adapter polls GET /company/jobs/{id} until the
record is delivered. A company on file and fresh answers at once. Nothing is cached on our side.

Field mapping mirrors tools/benchmark/bench_intake.py in the Pvalyou repo (the same record, the
same five fields), kept minimal here so it reads as one screen.
"""
from __future__ import annotations

import os
import re
import time
from typing import Any

from firmographic.common import CompanyCase, NormalizedCompany, ProviderResult

from .base import ProviderHTTPError, parse_employee_range, request_json_with_headers

VENDOR_SLUG = "pvalyou"
VENDOR_NAME = "Pvalyou"
REQUIRED_ENV = ("PVALYOU_API_KEY",)
PAID_CALLS_PER_CASE = 1

BASE = os.environ.get("PVALYOU_API_BASE", "https://company-file-api-559961100092.europe-west1.run.app/v1")
POLL_S = 10
MAX_WAIT_S = int(os.environ.get("PVALYOU_MAX_WAIT_S", "900"))   # a live read takes minutes, not ms


def _headers() -> dict[str, str]:
    return {"X-API-Key": os.environ["PVALYOU_API_KEY"], "Accept": "application/json",
            "Content-Type": "application/json", "X-Platform": "openbenchmarks"}


_LI = re.compile(r"linkedin\.com/(company|school|showcase)/([^/?#\s]+)", re.I)


def _linkedin(url: Any) -> str | None:
    """One spelling per page: the record can carry a country host (ar.linkedin.com) and a trailing
    slash, and the benchmark compares slugs on www.linkedin.com."""
    m = _LI.search(str(url or ""))
    return f"https://www.linkedin.com/{m.group(1).lower()}/{m.group(2).lower()}" if m else None


def _hq_city(record: dict[str, Any]) -> str | None:
    ents = record.get("legal_entities") or []
    for e in ents:
        if str(e.get("entity_hierarchy") or "").lower() == "headquarters" and e.get("city"):
            return e["city"]
    for e in ents:
        if e.get("city"):
            return e["city"]
    return None


def _normalize(record: dict[str, Any], case: CompanyCase) -> NormalizedCompany:
    ident = record.get("identity") or {}
    cls = record.get("classification") or {}
    prof = record.get("public_profiles") or {}
    low, high = parse_employee_range(ident.get("employee_range"))
    exact = ident.get("employees")
    if isinstance(exact, int) and exact > 0:
        low = high = exact          # an exact count scores on the benchmark's exact path
    industry = cls.get("industry")
    return NormalizedCompany(
        legal_name=ident.get("legal_name") or ident.get("name"),
        primary_domain=ident.get("domain") or case.input_domain,
        aliases=[a for a in (ident.get("other_legal_names") or []) if isinstance(a, str)],
        domains=[d for d in {ident.get("domain"), case.input_domain} if d],
        hq_country=ident.get("country"),
        hq_city=_hq_city(record),
        founded_year=ident.get("founding_year"),
        industry=industry,
        industries=[i for i in [industry, cls.get("sub_industry")] if i],
        linkedin_url=_linkedin(prof.get("linkedin")),
        headcount_min=low,
        headcount_max=high,
        funding_stage=None,
    )


def run(case: CompanyCase) -> ProviderResult:
    t0 = time.monotonic()
    try:
        body, latency_ms, _ = request_json_with_headers(
            "POST", f"{BASE}/company", headers=_headers(),
            json={"company": case.input_domain, "name": case.input_name, "tier": "basic"},
            timeout=60)
    except ProviderHTTPError as exc:
        if exc.status_code in (404, 422):
            return ProviderResult(VENDOR_SLUG, VENDOR_NAME, case.case_slug, "not_found",
                                  exc.elapsed_ms or 0, audit={"vendor_status_code": exc.status_code})
        raise
    job_id = (body or {}).get("job_id")
    while not (body or {}).get("record") and job_id:
        if time.monotonic() - t0 > MAX_WAIT_S:
            return ProviderResult(VENDOR_SLUG, VENDOR_NAME, case.case_slug, "error",
                                  int((time.monotonic() - t0) * 1000),
                                  error=f"no answer within {MAX_WAIT_S}s", audit={"job_id": job_id})
        if (body or {}).get("job_status") == "failed":
            return ProviderResult(VENDOR_SLUG, VENDOR_NAME, case.case_slug, "not_found",
                                  int((time.monotonic() - t0) * 1000),
                                  audit={"job_id": job_id, "outcome": body.get("error"),
                                         "message": body.get("message")})
        time.sleep(POLL_S)
        body, _, _ = request_json_with_headers("GET", f"{BASE}/company/jobs/{job_id}",
                                               headers=_headers(), timeout=60)
    record = (body or {}).get("record")
    total_ms = int((time.monotonic() - t0) * 1000)
    if not record:
        return ProviderResult(VENDOR_SLUG, VENDOR_NAME, case.case_slug, "not_found", total_ms,
                              audit={"job_id": job_id})
    billing = (body or {}).get("billing") or {}
    return ProviderResult(
        VENDOR_SLUG, VENDOR_NAME, case.case_slug, "ok", total_ms,
        normalized=_normalize(record, case),
        cost_units=billing.get("gross_usd"),          # the record's billing block: gross_usd + events
        cost_unit="usd" if billing.get("gross_usd") is not None else None,
        audit={"job_id": job_id, "org_id": record.get("org_id"), "outcome": record.get("outcome"),
               "record_as_of": record.get("record_as_of"), "first_response_ms": latency_ms},
    )
