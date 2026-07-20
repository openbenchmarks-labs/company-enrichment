"""Apply the v2 identity and headcount policy without vendor or LLM calls.

The existing snapshot already contains LLM judgments for qualitative
firmographic fields. This migration retains those judgments, materializes exact
LinkedIn employee counts from immutable RapidAPI responses, applies the
versioned redirect cache, and replaces only deterministic identity/headcount
verdicts plus their aggregate metrics.
"""
from __future__ import annotations

from collections import Counter
import hashlib

from firmographic.common import (
    DATASET_SLUG,
    HEADCOUNT_EXACT_TOLERANCE_PCT,
    IDENTITY_REDIRECTS_PATH,
    SCORED_ATTRIBUTES,
    CompanyCase,
    NormalizedCompany,
    ProviderResult,
    benchmark_metrics,
    headcount_match_basis,
    inactive_domain_reason,
    linkedin_match_basis,
    load_all_cases,
    now_iso,
    primary_domain_match_basis,
    recompute_leaderboard,
    read_snapshot,
    write_snapshot,
)


REPLACED_METRICS = {
    "reference_correct_primary_domain",
    "deterministic_reference_correct_primary_domain",
    "reference_correct_linkedin_url",
    "deterministic_reference_correct_linkedin_url",
    "reference_correct_headcount_band",
    "deterministic_reference_correct_headcount_band",
    "reference_accuracy_when_present_pct",
    "correct_field_yield_pct",
    "deterministic_reference_accuracy_when_present_pct",
    "deterministic_correct_field_yield_pct",
}


def _metric_map(metrics: list[dict]) -> dict[str, dict]:
    return {metric["metric_name"]: metric for metric in metrics}


def _result_from_run(run: dict) -> ProviderResult:
    normalized = (
        NormalizedCompany(**run["normalized"])
        if run.get("normalized")
        else None
    )
    return ProviderResult(
        provider_slug=run["provider_slug"],
        provider_name=run["provider_name"],
        case_slug=run["case_slug"],
        status=run["status"],
        latency_ms=run.get("latency_ms") or 0,
        normalized=normalized,
        cost_units=run.get("cost_units"),
        cost_unit=run.get("cost_unit"),
        ambiguity_count=run.get("ambiguity_count") or 0,
        audit=run.get("audit") or {},
        error=run.get("error"),
        queried_at=run.get("queried_at") or now_iso(),
    )


def _percentage(numerator: int, denominator: int) -> float | None:
    return round(100 * numerator / denominator, 2) if denominator else None


def refresh_run(case: CompanyCase, run: dict) -> None:
    result = _result_from_run(run)
    deterministic_metrics = benchmark_metrics(case, result)
    deterministic = _metric_map(deterministic_metrics)
    existing = _metric_map(run.get("metrics") or [])

    evaluable = [
        attribute
        for attribute in SCORED_ATTRIBUTES
        if f"reference_correct_{attribute}" in deterministic
    ]
    returned = [
        attribute
        for attribute in evaluable
        if deterministic[f"coverage_{attribute}"]["metric_value"] == 1
    ]
    deterministic_correct = {
        attribute: int(deterministic[f"reference_correct_{attribute}"]["metric_value"])
        for attribute in evaluable
    }
    final_correct = {
        attribute: (
            deterministic_correct[attribute]
            if attribute in {"primary_domain", "linkedin_url", "headcount_band"}
            else int(existing.get(f"reference_correct_{attribute}", deterministic[f"reference_correct_{attribute}"])["metric_value"])
        )
        for attribute in evaluable
    }
    base_policy_detail = {
        "reference_status": case.source_metadata.get("reference_status"),
        "input_domain_status": case.source_metadata.get("input_domain_status", "active_or_unknown"),
        "input_domain_inactive_reason": inactive_domain_reason(case.input_domain),
    }
    headcount_policy_detail = {
        **base_policy_detail,
        "judge_method": "deterministic_headcount_band_or_exact_count",
        "exact_employee_count_tolerance_pct": HEADCOUNT_EXACT_TOLERANCE_PCT,
        "match_basis": (
            headcount_match_basis(case.reference, result.normalized)
            if result.normalized
            else "not_returned"
        ),
    }

    retained = [
        metric for metric in run.get("metrics") or []
        if metric.get("metric_name") not in REPLACED_METRICS
    ]
    replacement_metrics = []
    if "primary_domain" in evaluable:
        domain_detail = {
            **base_policy_detail,
            "judge_method": "deterministic_verified_domain_redirect",
            "match_basis": (
                primary_domain_match_basis(case.reference, result.normalized)
                if result.normalized else "not_returned"
            ),
        }
        replacement_metrics.extend(
            [
                {
                    "metric_name": "deterministic_reference_correct_primary_domain",
                    "metric_value": deterministic_correct["primary_domain"],
                    "detail": domain_detail,
                },
                {
                    "metric_name": "reference_correct_primary_domain",
                    "metric_value": final_correct["primary_domain"],
                    "detail": domain_detail,
                },
            ]
        )
    if "linkedin_url" in evaluable:
        linkedin_detail = {
            **base_policy_detail,
            "judge_method": "deterministic_verified_linkedin_redirect",
            "match_basis": (
                linkedin_match_basis(case.reference, result.normalized)
                if result.normalized else "not_returned"
            ),
        }
        replacement_metrics.extend(
            [
                {
                    "metric_name": "deterministic_reference_correct_linkedin_url",
                    "metric_value": deterministic_correct["linkedin_url"],
                    "detail": linkedin_detail,
                },
                {
                    "metric_name": "reference_correct_linkedin_url",
                    "metric_value": final_correct["linkedin_url"],
                    "detail": linkedin_detail,
                },
            ]
        )
    if "headcount_band" in evaluable:
        replacement_metrics.extend(
            [
            {
                "metric_name": "deterministic_reference_correct_headcount_band",
                "metric_value": deterministic_correct["headcount_band"],
                "detail": headcount_policy_detail,
            },
            {
                "metric_name": "reference_correct_headcount_band",
                "metric_value": final_correct["headcount_band"],
                "detail": headcount_policy_detail,
            },
            ]
        )
    replacement_metrics.extend(
        [
            {
                "metric_name": "deterministic_reference_accuracy_when_present_pct",
                "metric_value": _percentage(sum(deterministic_correct.values()), len(returned)),
                "detail": {
                    **base_policy_detail,
                    "truth_fields_evaluable": len(evaluable),
                    "truth_fields_returned": len(returned),
                },
            },
            {
                "metric_name": "deterministic_correct_field_yield_pct",
                "metric_value": _percentage(sum(deterministic_correct.values()), len(evaluable)),
                "detail": {
                    **base_policy_detail,
                    "truth_fields_evaluable": len(evaluable),
                    "truth_fields_returned": len(returned),
                },
            },
            {
                "metric_name": "reference_accuracy_when_present_pct",
                "metric_value": _percentage(sum(final_correct.values()), len(returned)),
                "detail": {
                    **base_policy_detail,
                    "judge_method": "openai_structured_llm_with_deterministic_identity_and_headcount",
                    "truth_fields_evaluable": len(evaluable),
                    "truth_fields_returned": len(returned),
                },
            },
            {
                "metric_name": "correct_field_yield_pct",
                "metric_value": _percentage(sum(final_correct.values()), len(evaluable)),
                "detail": {
                    **base_policy_detail,
                    "judge_method": "openai_structured_llm_with_deterministic_identity_and_headcount",
                    "truth_fields_evaluable": len(evaluable),
                    "truth_fields_returned": len(returned),
                },
            },
        ]
    )
    retained.extend(replacement_metrics)
    run["metrics"] = retained


def main() -> int:
    snapshot = read_snapshot()
    cases, metadata = load_all_cases()
    if snapshot.get("case_count") != len(cases) or len(snapshot.get("runs") or []) != 2100:
        raise RuntimeError("expected the frozen 300-case, 2100-cell firmographic snapshot")
    by_slug = {case.case_slug: case for case in cases}
    for run in snapshot["runs"]:
        refresh_run(by_slug[run["case_slug"]], run)

    truth_counts = Counter(
        attribute
        for case in cases
        for attribute in SCORED_ATTRIBUTES
        if f"reference_correct_{attribute}" in _metric_map(
            benchmark_metrics(case, ProviderResult("truth", "Truth", case.case_slug, "ok", 0))
        )
    )
    snapshot.update(
        {
            "schema_version": "1.5",
            "dataset_slug": DATASET_SLUG,
            "cases": [case.to_dict() for case in cases],
            "source_manifests": metadata.get("source_manifests") or [],
            "reference_source": metadata["reference_source"],
            "reference_file": metadata["reference_file"],
            "reference_sha256": metadata["reference_sha256"],
            "reference_field_counts": dict(truth_counts),
            "evaluation_method": "openai_structured_llm_judge_with_deterministic_identity_and_headcount",
            "headcount_evaluation": {
                "band_match": "same canonical LinkedIn employee band (including 1-10 equivalent to LinkedIn 2-10)",
                "exact_count_match": "provider exact count within ±5% of LinkedIn exact count",
                "exact_employee_count_tolerance_pct": HEADCOUNT_EXACT_TOLERANCE_PCT,
            },
            "identity_evaluation": {
                "policy_file": str(IDENTITY_REDIRECTS_PATH.relative_to(IDENTITY_REDIRECTS_PATH.parents[2])),
                "policy_sha256": hashlib.sha256(IDENTITY_REDIRECTS_PATH.read_bytes()).hexdigest(),
                "domain_redirects": "audited domain redirects resolve before primary-domain scoring",
                "linkedin_redirects": "audited LinkedIn company-page redirects resolve before LinkedIn scoring",
                "inactive_inputs": "inactive inputs are retained for audit but excluded from primary-domain accuracy",
            },
            "updated_at": now_iso(),
        }
    )
    snapshot["leaderboard"] = recompute_leaderboard(snapshot["runs"], len(cases))
    write_snapshot(snapshot)
    print(
        "updated 300 cases and 2100 provider results; vendor calls=0, LLM calls=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
