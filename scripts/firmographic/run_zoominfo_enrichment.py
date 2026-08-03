#!/usr/bin/env python3
"""Checkpoint ZoomInfo GTM firmographic enrichment for the active cohort.

No funding fields are requested. One serial CLI request covers up to ten
domains, and the complete response is retained locally while each company gets
its own resumable checkpoint. This script never judges or writes benchmark DB
rows.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv
from supabase import create_client


ROOT = Path(__file__).resolve().parents[2]
DATASET_SLUG = "company-firmographic-enrichment-web-research-v2-293"
STAGING_REF = "ebyaszsocqvuptjihvhw"
OUTPUT = ROOT / "outputs" / "firmographic-web-research-v1" / "raw"
RAW = OUTPUT / "zoominfo"
BATCH_RAW = OUTPUT / "zoominfo-batches"
FIELDS = [
    "name", "website", "domainList", "city", "state", "country",
    "primaryIndustry", "industries", "employeeCount", "employeeRange",
    "foundedYear", "socialMediaUrls",
]
CHUNK_SIZE = 10


def now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def project_ref(url: str) -> str:
    return (urlparse(url).hostname or "").removesuffix(".supabase.co")


def load_cases() -> list[dict[str, str]]:
    load_dotenv(ROOT / ".env.local", override=True)
    url = os.getenv("NEXT_PUBLIC_SUPABASE_URL", "")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if project_ref(url) != STAGING_REF or not key:
        raise RuntimeError(".env.local must contain staging Supabase service-role credentials")
    client = create_client(url, key)
    dataset = client.table("datasets").select("id").eq("slug", DATASET_SLUG).single().execute().data
    rows: list[dict[str, Any]] = []
    for offset in range(0, 1_000, 200):
        page = client.table("firmographic_cases").select("case_slug,input_name,input_domain").eq("dataset_id", dataset["id"]).order("case_slug").range(offset, offset + 199).execute().data
        rows.extend(page)
        if len(page) < 200:
            break
    if len(rows) != 282 or len({row["input_domain"] for row in rows}) != 282:
        raise RuntimeError(f"expected 282 unique active cases, found {len(rows)}")
    return rows


def completed(case: dict[str, str]) -> bool:
    path = RAW / f"{case['case_slug']}.json"
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        return saved.get("input_domain") == case["input_domain"] and saved.get("status") in {"ok", "not_found"}
    except (FileNotFoundError, json.JSONDecodeError):
        return False


def invoke(cases: list[dict[str, str]]) -> tuple[dict[str, Any], int, str | None]:
    started = time.perf_counter()
    with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8") as handle:
        json.dump([{"domain": case["input_domain"]} for case in cases], handle)
        handle.flush()
        result = subprocess.run(
            ["gtm", "companies", "enrich", "--file", handle.name, "--fields", *FIELDS, "--format", "json"],
            capture_output=True, text=True, check=False, timeout=180,
        )
    latency_ms = round((time.perf_counter() - started) * 1000)
    if result.returncode:
        return {}, latency_ms, (result.stderr or result.stdout).strip() or f"gtm exited {result.returncode}"
    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        return {}, latency_ms, f"invalid JSON response: {error}"
    return response if isinstance(response, dict) else {}, latency_ms, None


def normalized(data: dict[str, Any]) -> dict[str, Any]:
    linkedin = next((str(row.get("url")) for row in data.get("socialMediaUrls") or [] if isinstance(row, dict) and str(row.get("type", "")).upper() == "LINKED_IN" and row.get("url")), None)
    def as_int(value: Any) -> int | None:
        try:
            return int(str(value).replace(",", "")) if value not in (None, "") else None
        except ValueError:
            return None
    employee_range = str(data.get("employeeRange") or "").replace(",", "")
    numbers = [int(value) for value in __import__("re").findall(r"\d+", employee_range)]
    # ZoomInfo expresses its top bucket as "Over 10,000". It is an
    # open-ended range, not the exact count 10,000; preserve that meaning for
    # the shared headcount judge.
    lowered_range = employee_range.lower()
    if numbers and ("over" in lowered_range or "more than" in lowered_range):
        minimum, maximum = numbers[0] + 1, None
    elif numbers and "+" in employee_range:
        minimum, maximum = numbers[0], None
    else:
        minimum, maximum = (numbers[0], numbers[1]) if len(numbers) >= 2 else (numbers[0], numbers[0]) if numbers else (None, None)
    return {
        "legal_name": data.get("name"), "primary_domain": data.get("domain") or data.get("website"),
        "domains": data.get("domainList") or [], "hq_country": data.get("country"), "hq_city": data.get("city"),
        "founded_year": as_int(data.get("foundedYear")), "industry": data.get("industry"),
        "industries": data.get("industries") or data.get("primaryIndustry") or [], "linkedin_url": linkedin,
        "headcount_min": minimum, "headcount_max": maximum, "headcount_exact": as_int(data.get("employeeCount")),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Make paid ZoomInfo requests; default only shows the plan.")
    parser.add_argument("--refresh-normalized", action="store_true", help="Reparse saved checkpoints only; makes no ZoomInfo calls.")
    parser.add_argument("--limit", type=int, help="Cap pending cases, for a deliberate partial run.")
    parser.add_argument("--interval-seconds", type=float, default=2, help="Pause between serial batches.")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    cases = load_cases()
    if args.refresh_normalized:
        refreshed = 0
        for case in cases:
            path = RAW / f"{case['case_slug']}.json"
            saved = json.loads(path.read_text(encoding="utf-8"))
            if saved.get("status") == "ok":
                data = ((saved.get("audit") or {}).get("raw_response") or {}).get("data") or {}
                previous = saved.get("normalized") or {}
                saved["normalized"] = normalized(data)
                write(path, saved)
                if previous != saved["normalized"]:
                    refreshed += 1
        print(json.dumps({"cohort": len(cases), "normalized_rows_changed": refreshed, "zoominfo_calls": 0}), flush=True)
        return 0
    pending = [case for case in cases if not completed(case)]
    if args.limit:
        pending = pending[:args.limit]
    plan = {"dataset_slug": DATASET_SLUG, "cohort": len(cases), "saved": len(cases) - len([case for case in cases if not completed(case)]), "pending": len(pending), "requests": (len(pending) + CHUNK_SIZE - 1) // CHUNK_SIZE, "batch_size": CHUNK_SIZE, "serial": True, "interval_seconds": args.interval_seconds, "fields": FIELDS, "funding_fields_requested": False, "output": str(OUTPUT.relative_to(ROOT)), "run": args.run}
    print(json.dumps(plan, indent=2), flush=True)
    if not args.run:
        return 0
    for offset in range(0, len(pending), CHUNK_SIZE):
        batch = pending[offset:offset + CHUNK_SIZE]
        response, latency_ms, failure = invoke(batch)
        batch_path = BATCH_RAW / f"batch-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}.json"
        write(batch_path, {"requested_domains": [case["input_domain"] for case in batch], "completed_at": now(), "latency_ms": latency_ms, "fields": FIELDS, "response": response, "error": failure})
        for index, case in enumerate(batch, start=1):
            item = response.get(f"company_{index}") if response else {}
            data = item.get("data") if isinstance(item, dict) else {}
            success = bool(isinstance(item, dict) and item.get("success") and isinstance(data, dict) and data)
            write(RAW / f"{case['case_slug']}.json", {"provider_slug": "zoominfo", "provider_name": "ZoomInfo", "case_slug": case["case_slug"], "input_name": case.get("input_name"), "input_domain": case["input_domain"], "status": "error" if failure else ("ok" if success else "not_found"), "error": failure or (None if success else "ZoomInfo returned no company match"), "latency_ms": latency_ms, "queried_at": now(), "requested_fields": FIELDS, "normalized": normalized(data) if success else {}, "audit": {"source": "zoominfo_gtm_companies_enrich", "batch_response_file": str(batch_path.relative_to(ROOT)), "match_status": data.get("matchStatus") if isinstance(data, dict) else None, "raw_response": item}})
        print(json.dumps({"completed": min(offset + len(batch), len(pending)), "of": len(pending), "batch_size": len(batch), "latency_ms": latency_ms, "error": failure}), flush=True)
        if offset + len(batch) < len(pending):
            time.sleep(args.interval_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
