#!/usr/bin/env python3
"""Verify the published v2 benchmark without network or paid API calls."""
from __future__ import annotations

import csv
import json
from collections import Counter
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from statistics import fmean, median


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "firmographic"
SNAPSHOT = ROOT / "data" / "latest-firmographic.json"
GROUND_TRUTH = DATA / "company-ground-truth-v2.json"
INPUTS = DATA / "company-inputs-v2.csv"
PROVIDERS = {
    "apollo", "company-enrich", "exa-research-v2", "explorium",
    "parallel-research", "people-data-labs", "predictleads-enrichment",
    "seltz-companies", "zoominfo",
}
SLICE_COUNTS = {
    "stable_large": 71,
    "long_tail": 88,
    "subsidiary": 71,
    "rebranded_or_domain_changed": 52,
}
PUBLISHED_YIELD = {
    "apollo": 88.58,
    "people-data-labs": 89.05,
    "parallel-research": 86.00,
    "predictleads-enrichment": 80.64,
    "explorium": 70.45,
    "company-enrich": 69.65,
    "zoominfo": 63.94,
    "exa-research-v2": 60.44,
    "seltz-companies": 29.98,
}


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def metric_value(run: dict, name: str) -> float | None:
    for metric in run.get("metrics") or []:
        if metric["metric_name"] == name and metric.get("metric_value") is not None:
            return float(metric["metric_value"])
    return None


def rounded(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def main() -> int:
    snapshot = load_json(SNAPSHOT)
    ground_truth = load_json(GROUND_TRUTH)
    cases, runs, leaderboard = snapshot["cases"], snapshot["runs"], snapshot["leaderboard"]

    assert snapshot["status"] == "complete"
    assert snapshot["dataset_slug"] == "company-firmographic-enrichment-web-research-v2-293"
    assert len(cases) == snapshot["case_count"] == 282
    assert len(runs) == 2538
    assert len({case["case_slug"] for case in cases}) == 282
    assert len({case["input_domain"] for case in cases}) == 282
    assert Counter(case["slice"] for case in cases) == Counter(SLICE_COUNTS)
    assert tuple(snapshot["scored_attributes"]) == (
        "hq_location", "founded_year", "industry", "linkedin_url", "headcount_band"
    )
    assert {run["provider_slug"] for run in runs} == PROVIDERS
    assert len({(run["case_slug"], run["provider_slug"]) for run in runs}) == 2538
    assert Counter(run["provider_slug"] for run in runs) == Counter({provider: 282 for provider in PROVIDERS})
    assert all(run["status"] in {"ok", "not_found"} for run in runs)

    assert ground_truth["status"] == "frozen"
    assert ground_truth["company_count"] == len(ground_truth["companies"]) == 282
    assert ground_truth["slice_counts"] == SLICE_COUNTS
    truth = {row["case_slug"]: row for row in ground_truth["companies"]}
    assert set(truth) == {case["case_slug"] for case in cases}
    for case in cases:
        row = truth[case["case_slug"]]
        assert row["input_domain"] == case["input_domain"]
        assert row["reference"] == case["reference"]
        assert row["ground_truth_status"] == "human_reviewed"

    with INPUTS.open(encoding="utf-8", newline="") as handle:
        inputs = list(csv.DictReader(handle))
    assert len(inputs) == 282
    assert {row["case_slug"] for row in inputs} == set(truth)
    assert len({row["input_domain"] for row in inputs}) == 282

    by_provider = {row["provider_slug"]: row for row in leaderboard}
    assert set(by_provider) == PROVIDERS
    for provider, expected in PUBLISHED_YIELD.items():
        provider_runs = [run for run in runs if run["provider_slug"] == provider]
        yields = [value for run in provider_runs if (value := metric_value(run, "correct_field_yield_pct")) is not None]
        accuracies = [value for run in provider_runs if (value := metric_value(run, "reference_accuracy_when_present_pct")) is not None]
        coverage = [value for run in provider_runs if (value := metric_value(run, "attribute_coverage_pct")) is not None]
        published = by_provider[provider]
        assert published["avg_correct_field_yield_pct"] == expected
        assert published["avg_correct_field_yield_pct"] == rounded(fmean(yields))
        assert published["avg_reference_accuracy_when_present_pct"] == rounded(fmean(accuracies))
        assert published["avg_attribute_coverage_pct"] == rounded(fmean(coverage))
        assert published["median_latency_ms"] == median(
            run["latency_ms"] for run in provider_runs if run["latency_ms"] is not None
        )

    print("final companies: 282")
    print("provider cells: 2538")
    print("providers: 9")
    print(f"slices: {dict(Counter(case['slice'] for case in cases))}")
    print("artifact verification passed; network calls: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
