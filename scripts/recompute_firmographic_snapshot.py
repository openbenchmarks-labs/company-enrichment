"""Apply the final cohort and recompute stored metrics without API calls."""
from __future__ import annotations

from collections import Counter

from firmographic.common import (
    DATASET_NAME,
    DATASET_SLUG,
    LINKEDIN_REFERENCE_STATUS,
    SCORED_ATTRIBUTES,
    NormalizedCompany,
    ProviderResult,
    benchmark_metrics,
    load_all_cases,
    now_iso,
    read_snapshot,
    recompute_leaderboard,
    write_snapshot,
)


def main() -> int:
    snapshot = read_snapshot()
    loaded_cases, reference_metadata = load_all_cases()
    cases = {case.case_slug: case for case in loaded_cases}
    runs = [
        row for row in snapshot.get("runs", []) if row.get("case_slug") in cases
    ]
    for row in runs:
        normalized = NormalizedCompany(**row["normalized"]) if row.get("normalized") else None
        result = ProviderResult(
            provider_slug=row["provider_slug"],
            provider_name=row["provider_name"],
            case_slug=row["case_slug"],
            status=row["status"],
            latency_ms=row.get("latency_ms") or 0,
            normalized=normalized,
            cost_units=row.get("cost_units"),
            cost_unit=row.get("cost_unit"),
            ambiguity_count=row.get("ambiguity_count") or 0,
            audit=row.get("audit") or {},
            error=row.get("error"),
            queried_at=row.get("queried_at") or now_iso(),
        )
        result.metrics = benchmark_metrics(cases[row["case_slug"]], result)
        row.clear()
        row.update(result.to_dict())
    truth_counts = Counter()
    for case in loaded_cases:
        reference = case.reference
        for attribute in SCORED_ATTRIBUTES:
            if attribute == "hq_location":
                available = bool(reference.get("hq_country") or reference.get("hq_city"))
            elif attribute == "headcount_band":
                available = (
                    reference.get("headcount_min") is not None
                    or reference.get("headcount_max") is not None
                )
            elif attribute == "industry":
                available = bool(reference.get("industry") or reference.get("industries"))
            else:
                available = reference.get(attribute) not in (None, "")
            truth_counts[attribute] += int(available)

    snapshot.update(
        {
            "schema_version": "1.3",
            "dataset_slug": DATASET_SLUG,
            "dataset_name": DATASET_NAME,
            "reference_status": LINKEDIN_REFERENCE_STATUS,
            "methodology_note": (
                "Available fields recorded from 300 identity-verified LinkedIn company "
                "pages are used as ground truth. Missing reference fields are excluded "
                "from denominators."
            ),
            "case_count": len(loaded_cases),
            "scored_attributes": list(SCORED_ATTRIBUTES),
            "reference_source": reference_metadata["reference_source"],
            "reference_file": reference_metadata["reference_file"],
            "reference_sha256": reference_metadata["reference_sha256"],
            "reference_field_counts": dict(truth_counts),
            "cases": [case.to_dict() for case in loaded_cases],
            "runs": runs,
        }
    )
    for legacy_key in ("source_manifests", "source_cohort_slug", "source_cohort_sha256"):
        snapshot.pop(legacy_key, None)
    snapshot["leaderboard"] = recompute_leaderboard(runs, len(loaded_cases))
    provider_slugs = {row["provider_slug"] for row in runs}
    status_counts = Counter(row.get("status") or "unknown" for row in runs)
    target_cells = len(loaded_cases) * len(provider_slugs)
    snapshot["execution_summary"] = {
        "selected_provider_count": len(provider_slugs),
        "selected_target_cells": target_cells,
        "target_cells": target_cells,
        "recorded_cells": len(runs),
        "missing_cells": target_cells - len(runs),
        "status_counts": dict(status_counts),
        "retryable_errors": sum(
            1
            for row in runs
            if row.get("status") == "error" and (row.get("audit") or {}).get("retriable")
        ),
        "nonretryable_errors": sum(
            1
            for row in runs
            if row.get("status") == "error" and not (row.get("audit") or {}).get("retriable")
        ),
        "stopped_providers": {},
        "automatic_retries": 0,
    }
    snapshot["status"] = (
        "complete"
        if target_cells == len(runs) and not status_counts.get("error")
        else "needs_retry"
    )
    snapshot["updated_at"] = now_iso()
    write_snapshot(snapshot)
    print(
        f"froze {len(loaded_cases)} cases and recomputed {len(runs)} firmographic cells; "
        "0 vendor calls"
    )
    print(
        "truth fields: "
        + ", ".join(f"{field}={truth_counts[field]}" for field in SCORED_ATTRIBUTES)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
