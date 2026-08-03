#!/usr/bin/env python3
"""Derive public input and Ground Truth artifacts from the published v2 snapshot.

The snapshot is the source of truth. This export deliberately omits case source
metadata and provider audit payloads from the two compact, easy-to-inspect
artifacts it creates.
"""
from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "data" / "latest-firmographic.json"
DATA = ROOT / "data" / "firmographic"
INPUTS = DATA / "company-inputs-v2.csv"
GROUND_TRUTH = DATA / "company-ground-truth-v2.json"


def main() -> int:
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    cases = snapshot["cases"]
    INPUTS.parent.mkdir(parents=True, exist_ok=True)
    with INPUTS.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["case_slug", "slice", "input_name", "input_domain", "linkedin_url"],
        )
        writer.writeheader()
        for case in cases:
            writer.writerow({
                "case_slug": case["case_slug"],
                "slice": case["slice"],
                "input_name": case.get("input_name") or "",
                "input_domain": case["input_domain"],
                "linkedin_url": (case.get("reference") or {}).get("linkedin_url") or "",
            })

    companies = [
        {
            "case_slug": case["case_slug"],
            "input_name": case.get("input_name"),
            "input_domain": case["input_domain"],
            "slice": case["slice"],
            "ground_truth_status": "human_reviewed",
            "reference": case.get("reference") or {},
        }
        for case in cases
    ]
    payload = {
        "schema_version": 2,
        "dataset_slug": snapshot["dataset_slug"],
        "status": "frozen",
        "created_at": snapshot.get("updated_at"),
        "method": (
            "Human-refreshed company references across official sources, filings, "
            "registries, news, reputable reference sources, redirects, and canonical "
            "or alternate LinkedIn URLs."
        ),
        "company_count": len(companies),
        "slice_counts": dict(Counter(case["slice"] for case in cases)),
        "scored_attributes": snapshot["scored_attributes"],
        "companies": companies,
    }
    GROUND_TRUTH.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {INPUTS.relative_to(ROOT)} and {GROUND_TRUTH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
