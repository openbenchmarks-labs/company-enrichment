#!/usr/bin/env python3
"""Checkpoint TinyFish company enrichment for the active cohort.

Calls the public TinyFish company API (https://api.aitinyfish.com/company)
once per case, serially, and writes one resumable checkpoint per company. The
API key is read from the untracked repo-root .env.local (AITINYFISH_API_KEY)
and is never committed. This script never judges or writes benchmark DB rows.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[2]
INPUTS = ROOT / "data" / "firmographic" / "company-inputs-v2.csv"
OUTPUT = ROOT / "outputs" / "firmographic-web-research-v1" / "raw"
RAW = OUTPUT / "tinyfish"
PROVIDER_SLUG = "tinyfish"
PROVIDER_NAME = "TinyFish"


def now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_cases() -> list[dict[str, str]]:
    with INPUTS.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != 282 or len({row["input_domain"] for row in rows}) != 282:
        raise RuntimeError(f"expected 282 unique active cases, found {len(rows)}")
    return rows


def load_config() -> tuple[str, str]:
    load_dotenv(ROOT / ".env.local", override=True)
    key = os.getenv("AITINYFISH_API_KEY", "")
    if not key:
        raise RuntimeError(".env.local must contain AITINYFISH_API_KEY")
    base = os.getenv("AITINYFISH_API_BASE", "https://api.aitinyfish.com").rstrip("/")
    return base, key


def completed(case: dict[str, str]) -> bool:
    path = RAW / f"{case['case_slug']}.json"
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        return saved.get("input_domain") == case["input_domain"] and saved.get("status") in {"ok", "not_found"}
    except (FileNotFoundError, json.JSONDecodeError):
        return False


def invoke(base: str, key: str, case: dict[str, str]) -> tuple[dict[str, Any], int, str | None]:
    from urllib.parse import urlencode

    query = {"domain": case["input_domain"]}
    if case.get("input_name"):
        query["name"] = case["input_name"]
    request = urllib.request.Request(
        f"{base}/company?{urlencode(query)}",
        headers={"x-api-key": key, "accept": "application/json"},
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=420) as response:
            body = response.read().decode("utf-8")
            status = response.status
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        latency_ms = round((time.perf_counter() - started) * 1000)
        return {}, latency_ms, f"http_{error.code}: {body[:200]}"
    except (urllib.error.URLError, TimeoutError) as error:
        latency_ms = round((time.perf_counter() - started) * 1000)
        return {}, latency_ms, f"request failed: {error}"
    latency_ms = round((time.perf_counter() - started) * 1000)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as error:
        return {}, latency_ms, f"invalid JSON response: {error}"
    if status != 200:
        return {}, latency_ms, f"http_{status}: {body[:200]}"
    return payload, latency_ms, None


def normalized(company: dict[str, Any] | None) -> dict[str, Any]:
    company = company or {}
    return {
        "legal_name": company.get("legal_name"),
        "primary_domain": company.get("primary_domain"),
        "domains": [company["primary_domain"]] if company.get("primary_domain") else [],
        "hq_country": company.get("hq_country"),
        "hq_city": company.get("hq_city"),
        "founded_year": company.get("founded_year"),
        "industry": company.get("industry"),
        "industries": [company["industry"]] if company.get("industry") else [],
        "linkedin_url": company.get("linkedin_url"),
        "headcount_min": company.get("headcount_min"),
        "headcount_max": company.get("headcount_max"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Make paid TinyFish API requests; default only shows the plan.")
    parser.add_argument("--limit", type=int, help="Cap pending cases, for a deliberate partial run.")
    parser.add_argument("--interval-seconds", type=float, default=1, help="Pause between serial requests.")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    cases = load_cases()
    base, key = load_config()
    incomplete = [case for case in cases if not completed(case)]
    pending = incomplete[: args.limit] if args.limit else incomplete
    plan = {
        "cohort": len(cases),
        "saved": len(cases) - len(incomplete),
        "pending": len(pending),
        "requests": len(pending),
        "serial": True,
        "interval_seconds": args.interval_seconds,
        "api_base": base,
        "output": str(OUTPUT.relative_to(ROOT)),
        "run": args.run,
    }
    print(json.dumps(plan, indent=2), flush=True)
    if not args.run:
        return 0
    for index, case in enumerate(pending, start=1):
        payload, latency_ms, failure = invoke(base, key, case)
        company = payload.get("company") if isinstance(payload, dict) else None
        status = "error" if failure else ("ok" if company else "not_found")
        write(
            RAW / f"{case['case_slug']}.json",
            {
                "provider_slug": PROVIDER_SLUG,
                "provider_name": PROVIDER_NAME,
                "case_slug": case["case_slug"],
                "input_name": case.get("input_name"),
                "input_domain": case["input_domain"],
                "status": status,
                "error": failure,
                "latency_ms": latency_ms,
                "queried_at": now(),
                "normalized": normalized(company) if company else {},
                "audit": {"source": "tinyfish_company_api", "endpoint": f"{base}/company"},
            },
        )
        print(json.dumps({"completed": index, "of": len(pending), "case_slug": case["case_slug"], "status": status, "latency_ms": latency_ms, "error": failure}), flush=True)
        if index < len(pending):
            time.sleep(args.interval_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
