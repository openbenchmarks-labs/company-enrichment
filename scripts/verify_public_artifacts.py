#!/usr/bin/env python3
"""Verify the committed benchmark artifacts without network or paid API calls."""
from __future__ import annotations

import csv
import json
from collections import Counter
from decimal import ROUND_HALF_UP, Decimal
from statistics import fmean, median
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "firmographic"
SNAPSHOT = ROOT / "data" / "latest-firmographic.json"
GROUND_TRUTH = DATA / "company-ground-truth-v1.json"
INPUTS = DATA / "company-inputs-v1.csv"
GENERAL_JUDGE = DATA / "llm-judge-v3"
PROVIDERS = {
    "apollo", "company-enrich", "explorium", "fiber", "ocean",
    "people-data-labs", "predictleads",
}
SLICE_COUNTS = {
    "stable_large": 78,
    "long_tail": 89,
    "subsidiary": 80,
    "rebranded_or_domain_changed": 53,
}
PUBLISHED_YIELD = {
    "fiber": 91.91,
    "people-data-labs": 86.93,
    "ocean": 84.54,
    "apollo": 84.12,
    "predictleads": 83.54,
    "explorium": 73.27,
    "company-enrich": 71.25,
}


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def metric_value(run: dict, metric_name: str) -> float | None:
    for metric in run["metrics"]:
        if metric["metric_name"] == metric_name:
            return float(metric["metric_value"])
    return None


def snapshot_round(value: float, places: int = 2) -> float:
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def main() -> int:
    snapshot = load_json(SNAPSHOT)
    ground_truth = load_json(GROUND_TRUTH)
    cases = snapshot["cases"]
    runs = snapshot["runs"]
    leaderboard = snapshot["leaderboard"]

    assert snapshot["status"] == "complete"
    assert snapshot["dataset_slug"] == "company-firmographic-linkedin-gt-2026-q3-v2"
    assert len(cases) == snapshot["case_count"] == 300
    assert len(runs) == 2100
    assert len({case["case_slug"] for case in cases}) == 300
    assert len({case["input_domain"] for case in cases}) == 300
    assert Counter(case["slice"] for case in cases) == Counter(SLICE_COUNTS)
    assert {run["provider_slug"] for run in runs} == PROVIDERS
    assert len({(run["case_slug"], run["provider_slug"]) for run in runs}) == 2100
    assert Counter(run["provider_slug"] for run in runs) == Counter({p: 300 for p in PROVIDERS})
    assert all(run["status"] in {"ok", "not_found"} for run in runs)

    assert ground_truth["status"] == "frozen"
    assert ground_truth["company_count"] == len(ground_truth["companies"]) == 300
    assert ground_truth["slice_counts"] == SLICE_COUNTS
    truth_by_slug = {row["case_slug"]: row for row in ground_truth["companies"]}
    assert len(truth_by_slug) == 300
    for case in cases:
        truth = truth_by_slug[case["case_slug"]]
        assert truth["input_domain"] == case["input_domain"]
        assert truth["slice"] == case["slice"]
        # The published snapshot shares a cross-benchmark schema and may carry
        # unscored null fields (for example, funding_stage). Every firmographic
        # reference field in the frozen ground-truth file must still agree.
        assert truth["reference"] == {
            field: case["reference"].get(field)
            for field in truth["reference"]
        }
        assert truth["website_linkedin_identity"] == "human_verified"

    with INPUTS.open(encoding="utf-8", newline="") as handle:
        inputs = list(csv.DictReader(handle))
    assert len(inputs) == 300
    assert {row["case_slug"] for row in inputs} == set(truth_by_slug)
    assert len({row["input_domain"] for row in inputs}) == 300

    leaderboard_by_provider = {row["provider_slug"]: row for row in leaderboard}
    assert set(leaderboard_by_provider) == PROVIDERS
    for provider, expected in PUBLISHED_YIELD.items():
        provider_runs = [run for run in runs if run["provider_slug"] == provider]
        published = leaderboard_by_provider[provider]
        yields = [
            value for run in provider_runs
            if (value := metric_value(run, "correct_field_yield_pct")) is not None
        ]
        accuracies = [
            value for run in provider_runs
            if (value := metric_value(run, "reference_accuracy_when_present_pct")) is not None
        ]
        coverage = [
            value for run in provider_runs
            if (value := metric_value(run, "attribute_coverage_pct")) is not None
        ]
        assert published["avg_correct_field_yield_pct"] == expected
        assert published["avg_correct_field_yield_pct"] == snapshot_round(
            fmean(yields)
        )
        assert published["avg_reference_accuracy_when_present_pct"] == snapshot_round(
            fmean(accuracies)
        )
        assert published["avg_attribute_coverage_pct"] == snapshot_round(
            fmean(coverage)
        )
        assert published["median_latency_ms"] == median(
            run["latency_ms"] for run in provider_runs if run["latency_ms"] is not None
        )

    general_providers = {
        path.stem: load_json(path)
        for path in (GENERAL_JUDGE / "providers").glob("*.json")
    }
    assert set(general_providers) == PROVIDERS
    assert all(
        len(record["judgments"]) == record["case_count"] == 300
        for record in general_providers.values()
    )
    assert {record["prompt_version"] for record in general_providers.values()} == {
        "firmographic-provider-chunk-judge-v3.0"
    }
    assert all(record["chunk_count"] == 6 for record in general_providers.values())
    assert len(list((GENERAL_JUDGE / "chunks").glob("*/*.json"))) == 42
    general_manifest = load_json(GENERAL_JUDGE / "manifest.json")
    assert general_manifest["paid_calls_total"] == 42
    assert general_manifest["paid_call_design"] == (
        "checkpointed fixed-size company chunks, one provider per call"
    )

    status_counts = Counter(run["status"] for run in runs)
    print("final companies: 300")
    print("provider cells: 2100")
    print(f"providers: {len(PROVIDERS)}")
    print(f"slices: {dict(Counter(case['slice'] for case in cases))}")
    print(f"statuses: {dict(sorted(status_counts.items()))}")
    print("general judge calls: 42 (v3, six chunks × seven providers)")
    print("artifact verification passed; network calls: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
