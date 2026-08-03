#!/usr/bin/env python3
"""Locally rejudge every enrichment vendor's headcount with GPT-5.6 Sol.

This produces a reviewable v2 headcount baseline only. It reads current
staging Ground Truth and vendor normalized outputs, adds the locally collected
ZoomInfo outputs, and writes local checkpoints. It never updates benchmark DB
metrics, snapshots, or leaderboards.
"""
from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel
from supabase import create_client

from field_judge_prompts import HEADCOUNT_SOL_PROMPT, HEADCOUNT_SOL_PROMPT_VERSION


ROOT = Path(__file__).resolve().parents[2]
DATASET_SLUG = "company-firmographic-enrichment-web-research-v2-293"
STAGING_REF = "ebyaszsocqvuptjihvhw"
MODEL = "gpt-5.6-sol"
REASONING_EFFORT = "medium"
RAW_ZOOMINFO = ROOT / "outputs" / "firmographic-web-research-v1" / "raw" / "zoominfo"
OUT = ROOT / "outputs" / "firmographic-web-research-v1" / "headcount-sol-v2"
FIELD = "headcount_band"


class Verdict(BaseModel):
    case_slug: str
    provider_present: bool
    is_correct: bool
    rationale: str


class Output(BaseModel):
    provider_slug: str
    verdicts: list[Verdict]


def project_ref(url: str) -> str:
    return (urlparse(url).hostname or "").removesuffix(".supabase.co")


def fetch_all(client: Any, table: str, dataset_id: str, fields: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for offset in range(0, 10_000, 500):
        page = client.table(table).select(fields).eq("dataset_id", dataset_id).order("id").range(offset, offset + 499).execute().data
        rows.extend(page)
        if len(page) < 500:
            return rows
    raise RuntimeError(f"pagination overflow for {table}")


def value(record: dict[str, Any] | None) -> dict[str, int | None] | None:
    if not record:
        return None
    result = {"min": record.get("headcount_min"), "max": record.get("headcount_max")}
    return result if any(item is not None for item in result.values()) else None


def load_inputs() -> tuple[list[dict[str, Any]], dict[str, dict[str, dict[str, Any]]]]:
    load_dotenv(ROOT / ".env.local", override=True)
    url, key = os.getenv("NEXT_PUBLIC_SUPABASE_URL", ""), os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if project_ref(url) != STAGING_REF or not key:
        raise RuntimeError(".env.local must contain staging Supabase service-role credentials")
    client = create_client(url, key)
    dataset = client.table("datasets").select("id").eq("slug", DATASET_SLUG).single().execute().data
    cases = fetch_all(client, "firmographic_cases", dataset["id"], "id,case_slug,reference_attributes")
    runs = fetch_all(client, "firmographic_runs", dataset["id"], "case_id,status,normalized_response,providers(slug,name)")
    if len(cases) != 282:
        raise RuntimeError(f"expected 282 cases, found {len(cases)}")
    slug_by_case_id = {case["id"]: case["case_slug"] for case in cases}
    providers: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for run in runs:
        provider = run.get("providers") or {}
        if isinstance(provider, list):
            provider = provider[0] if provider else {}
        slug = provider.get("slug")
        case_slug = slug_by_case_id.get(run["case_id"])
        if slug and case_slug:
            providers[slug][case_slug] = {"status": run["status"], "normalized": run.get("normalized_response") or {}}
    if len(providers) != 7 or any(len(rows) != 282 for rows in providers.values()):
        raise RuntimeError(f"expected seven complete staging providers, found { {slug: len(rows) for slug, rows in providers.items()} }")
    zoominfo: dict[str, dict[str, Any]] = {}
    for case in cases:
        path = RAW_ZOOMINFO / f"{case['case_slug']}.json"
        row = json.loads(path.read_text(encoding="utf-8"))
        zoominfo[case["case_slug"]] = {"status": row["status"], "normalized": row.get("normalized") or {}}
    if len(zoominfo) != 282:
        raise RuntimeError("incomplete local ZoomInfo outputs")
    providers["zoominfo"] = zoominfo
    return cases, dict(providers)


def rows_for_provider(cases: list[dict[str, Any]], provider_rows: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for case in cases:
        reference = case.get("reference_attributes") or {}
        expected = value(reference)
        if expected is None:
            continue
        provider = provider_rows[case["case_slug"]]
        rows.append({"case_slug": case["case_slug"], "reference": expected, "provider_value": value(provider["normalized"]), "reference_exact_headcount": reference.get("headcount_exact")})
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Make the per-provider Sol judge calls; default prints the plan.")
    parser.add_argument("--force", action="store_true", help="Overwrite saved provider checkpoints.")
    args = parser.parse_args()
    cases, providers = load_inputs()
    payloads = {slug: rows_for_provider(cases, rows) for slug, rows in providers.items()}
    if any(len(rows) != 280 for rows in payloads.values()):
        raise RuntimeError(f"unexpected headcount denominator: { {slug: len(rows) for slug, rows in payloads.items()} }")
    plan = {"dataset_slug": DATASET_SLUG, "providers": sorted(payloads), "calls": len(payloads), "records_per_call": 280, "model": MODEL, "reasoning_effort": REASONING_EFFORT, "prompt_version": HEADCOUNT_SOL_PROMPT_VERSION, "run": args.run}
    print(json.dumps(plan, indent=2), flush=True)
    if not args.run:
        return 0
    load_dotenv(ROOT / ".env.local", override=False)
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is required")
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], max_retries=0, timeout=900)
    for slug, rows in sorted(payloads.items()):
        path = OUT / slug / f"{FIELD}.json"
        if path.exists() and not args.force:
            print(json.dumps({"provider": slug, "status": "saved_checkpoint_reused"}), flush=True)
            continue
        response = client.responses.parse(
            model=MODEL, reasoning={"effort": REASONING_EFFORT},
            input=[{"role": "system", "content": HEADCOUNT_SOL_PROMPT}, {"role": "user", "content": json.dumps({"provider_slug": slug, "field": FIELD, "cases": rows}, separators=(",", ":"))}],
            text_format=Output, max_output_tokens=50_000, store=False,
        )
        result = response.output_parsed
        expected = {row["case_slug"] for row in rows}
        if not result or result.provider_slug != slug or {row.case_slug for row in result.verdicts} != expected or len(result.verdicts) != len(rows):
            raise RuntimeError(f"invalid headcount verdicts for {slug}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"model": MODEL, "reasoning_effort": REASONING_EFFORT, "prompt_version": HEADCOUNT_SOL_PROMPT_VERSION, "provider_slug": slug, "field": FIELD, "response_id": response.id, "verdicts": [row.model_dump() for row in result.verdicts]}, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"provider": slug, "eligible": len(rows), "correct": sum(row.is_correct for row in result.verdicts), "present": sum(row.provider_present for row in result.verdicts), "response_id": response.id}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
