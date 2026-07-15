"""Rejudge firmographic industry fields with a dedicated semantic contract.

Each paid request contains every company with non-null industry ground truth for
one provider. Provider batches are checkpointed independently. The apply step
changes only industry correctness and the aggregate scores derived from it.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import threading
import time
from collections import Counter
from pathlib import Path
from statistics import mean
from typing import Any, Literal
from os.path import commonprefix

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field

from firmographic.common import (
    LINKEDIN_REFERENCE_STATUS,
    SCORED_ATTRIBUTES,
    SNAPSHOT_PATH,
    now_iso,
    read_snapshot,
    recompute_leaderboard,
    write_snapshot,
)


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "data" / "firmographic" / "llm-industry-judge-v1"
PROVIDER_DIR = OUTPUT_DIR / "providers"
ERROR_DIR = OUTPUT_DIR / "errors"
MANIFEST_PATH = OUTPUT_DIR / "manifest.json"
SUMMARY_PATH = OUTPUT_DIR / "summary.json"
PROMPT_VERSION = "firmographic-industry-judge-v1.0"
DEFAULT_MODEL = "gpt-5.6"
DEFAULT_REASONING_EFFORT = "high"
PROVIDER_SLUGS = (
    "apollo",
    "company-enrich",
    "explorium",
    "fiber",
    "ocean",
    "people-data-labs",
    "predictleads",
)

# Explicit contract resolutions discovered during the post-run audit. The raw
# model outputs remain immutable in provider checkpoints; these overrides make
# the applied metric follow the benchmark's stated broader-but-consistent rule.
POLICY_OVERRIDES: dict[tuple[str, str], dict[str, Any]] = {
    ("company-enrich", "ww_bca426ed7f6f2c51ba05caf7ed8ef22b"): {
        "is_correct": False,
        "relationship": "contradictory_or_unrelated",
        "rationale": "Higher Education conflicts with the provider's explicit K-12 Schools label.",
    },
    ("explorium", "ww_5ab3fa43bf1cdfe8f008a84737679ab0"): {
        "is_correct": True,
        "relationship": "same_family",
        "rationale": "Administration of Education Programs is consistent with primary and secondary education.",
    },
    ("explorium", "ww_5cca48cb4250836e4203b69ee8a2c685"): {
        "is_correct": True,
        "relationship": "same_family",
        "rationale": "Administration of Education Programs is consistent with primary and secondary education.",
    },
}

SYSTEM_PROMPT = """You are the dedicated industry-field judge for a company-enrichment benchmark.

For every supplied case, compare the authoritative reference industry with the provider's one or
more industry labels. Return one typed yes/no verdict for every case. Use only the supplied labels;
do not add outside company knowledge and do not override the reference.

The benchmark intentionally treats broader and narrower classifications as correct when they are
consistent descriptions of the same primary industry family. Taxonomy granularity is not an error.

MATCH when any meaningful provider label is:
- the same label, a conventional synonym, or a renamed taxonomy label;
- a broader umbrella category that consistently contains the reference industry; or
- a narrower subsector or operating specialization consistent with the reference industry.

Examples that MATCH:
- Software Development <> IT Services and IT Consulting
- Home Health Care Services <> Hospitals and Health Care
- Truck Transportation <> Transportation, Logistics, Supply Chain and Storage
- Movies and Sound Recording <> Entertainment Providers
- Marketing Services <> Market Research
- Education <> Higher Education
- Machinery Manufacturing <> Manufacturing
- Renewable Energy Semiconductor Manufacturing <> Renewables and Environment

MISMATCH when the labels identify materially different industries or contradictory specific
subsectors. Sharing only a generic word is insufficient. Examples that MISMATCH:
- Higher Education <> K-12 Schools
- Aviation component manufacturing <> Airlines
- Electronics manufacturing <> electronics wholesaling
- Vehicle repair <> health care
- Public safety <> telecommunications

When the provider supplies multiple labels, match if at least one substantive label is consistent
with the reference. Ignore unrelated auxiliary tags unless a precise provider label directly
contradicts the reference and no other label supports it. A missing provider industry is always
provider_present=false and is_correct=false.

Use relationship=same_family for acceptable broader/narrower mappings. Keep the rationale short
and name the label relationship that determined the verdict. Do not omit or duplicate cases."""


Relationship = Literal[
    "not_returned",
    "exact_or_synonym",
    "same_family",
    "contradictory_or_unrelated",
]


class IndustryJudgment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_slug: str
    provider_present: bool
    is_correct: bool
    relationship: Relationship
    confidence: int = Field(ge=0, le=100)
    rationale: str = Field(min_length=1, max_length=180)


class ProviderIndustryOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_slug: str
    cases: list[IndustryJudgment]


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _prompt_sha256() -> str:
    return hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()


def _provider_path(provider_slug: str) -> Path:
    return PROVIDER_DIR / f"{provider_slug}.json"


def _error_path(provider_slug: str) -> Path:
    return ERROR_DIR / f"{provider_slug}.json"


def _metric_map(run: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {metric["metric_name"]: metric for metric in run.get("metrics") or []}


def _industry_values(company: dict[str, Any] | None) -> list[str] | None:
    if not company:
        return None
    values = [company.get("industry"), *(company.get("industries") or [])]
    values = list(
        dict.fromkeys(
            str(value).strip()
            for value in values
            if value is not None and str(value).strip()
        )
    )
    return values or None


def _build_provider_input(
    snapshot: dict[str, Any],
    provider_slug: str,
    *,
    selected_case_slugs: list[str] | None = None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    runs = {
        run["case_slug"]: run
        for run in snapshot["runs"]
        if run["provider_slug"] == provider_slug
    }
    cases = snapshot["cases"]
    if len(runs) != len(cases):
        raise ValueError(f"{provider_slug} has {len(runs)}/{len(cases)} runs")

    selected = set(selected_case_slugs) if selected_case_slugs is not None else None
    expected: dict[str, dict[str, Any]] = {}
    rows = []
    for case in cases:
        case_slug = case["case_slug"]
        if selected is not None and case_slug not in selected:
            continue
        reference_values = _industry_values(case.get("reference"))
        if not reference_values:
            continue
        run = runs[case_slug]
        metrics = _metric_map(run)
        provider_values = _industry_values(run.get("normalized"))
        coverage = metrics.get("coverage_industry", {}).get("metric_value")
        if coverage == 0:
            provider_values = None
        current = metrics.get("reference_correct_industry", {}).get("metric_value")
        deterministic = metrics.get("deterministic_reference_correct_industry", {}).get(
            "metric_value"
        )
        expected[case_slug] = {
            "reference": reference_values,
            "provider_value": provider_values,
            "provider_present": provider_values is not None,
            "general_llm_is_correct": None if current is None else bool(current),
            "deterministic_is_correct": None if deterministic is None else bool(deterministic),
        }
        rows.append(
            {
                "case_slug": case_slug,
                "reference_industries": reference_values,
                "provider_industries": provider_values,
            }
        )
    return {"provider_slug": provider_slug, "cases": rows}, expected


def _validate_output(
    output: ProviderIndustryOutput,
    *,
    provider_slug: str,
    expected: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    if output.provider_slug != provider_slug:
        raise ValueError(f"judge returned provider {output.provider_slug}, expected {provider_slug}")
    returned: dict[str, IndustryJudgment] = {}
    for judgment in output.cases:
        if judgment.case_slug in returned:
            raise ValueError(f"judge duplicated {judgment.case_slug}")
        returned[judgment.case_slug] = judgment
    if set(returned) != set(expected):
        missing = sorted(set(expected) - set(returned))
        extra = sorted(set(returned) - set(expected))
        # Long opaque case hashes are occasionally copied with a corrupted
        # suffix. Repair only an unambiguous one-for-one typo with a long
        # matching prefix; every other case and every field still validates.
        if (
            len(missing) == 1
            and len(extra) == 1
            and missing[0].startswith("ww_")
            and extra[0].startswith("ww_")
            and len(commonprefix([missing[0], extra[0]])) >= 20
        ):
            returned[missing[0]] = returned.pop(extra[0]).model_copy(
                update={"case_slug": missing[0]}
            )
        else:
            raise ValueError(f"case-set mismatch: missing={missing[:5]}, extra={extra[:5]}")

    validated = []
    for case_slug, source in expected.items():
        judgment = returned[case_slug]
        if judgment.provider_present != source["provider_present"]:
            raise ValueError(f"presence mismatch for {case_slug}")
        if not judgment.provider_present:
            if judgment.is_correct or judgment.relationship != "not_returned":
                raise ValueError(f"invalid absent verdict for {case_slug}")
        elif judgment.relationship == "not_returned":
            raise ValueError(f"present value labelled not_returned for {case_slug}")
        elif judgment.is_correct and judgment.relationship == "contradictory_or_unrelated":
            raise ValueError(f"correct contradiction for {case_slug}")
        elif not judgment.is_correct and judgment.relationship in {
            "exact_or_synonym",
            "same_family",
        }:
            raise ValueError(f"mismatch labelled as matching relationship for {case_slug}")
        validated.append(
            {
                **judgment.model_dump(),
                **source,
                "changed_from_general_llm": judgment.is_correct
                != source["general_llm_is_correct"],
                "changed_from_deterministic": judgment.is_correct
                != source["deterministic_is_correct"],
            }
        )
    return validated


def _usage(response: Any) -> dict[str, Any]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    return usage.model_dump(mode="json") if hasattr(usage, "model_dump") else dict(usage)


def _judge_provider(
    client: OpenAI,
    *,
    snapshot: dict[str, Any],
    provider_slug: str,
    model: str,
    reasoning_effort: str,
    source_snapshot_sha256: str,
    selected_case_slugs: list[str] | None = None,
    checkpoint_path: Path | None = None,
) -> dict[str, Any]:
    request_input, expected = _build_provider_input(
        snapshot,
        provider_slug,
        selected_case_slugs=selected_case_slugs,
    )
    started = time.perf_counter()
    response = client.responses.parse(
        model=model,
        reasoning={"effort": reasoning_effort},
        input=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(request_input, ensure_ascii=False, separators=(",", ":")),
            },
        ],
        text_format=ProviderIndustryOutput,
        max_output_tokens=30_000,
        store=False,
    )
    latency_ms = round((time.perf_counter() - started) * 1000)
    if response.output_parsed is None:
        raise ValueError("OpenAI response did not contain parsed structured output")
    judgments = _validate_output(
        response.output_parsed,
        provider_slug=provider_slug,
        expected=expected,
    )
    record = {
        "schema_version": 1,
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": _prompt_sha256(),
        "source_snapshot_sha256": source_snapshot_sha256,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "provider_slug": provider_slug,
        "judged_at": now_iso(),
        "latency_ms": latency_ms,
        "response_id": response.id,
        "usage": _usage(response),
        "case_count": len(judgments),
        "judgments": judgments,
    }
    _atomic_json(checkpoint_path or _provider_path(provider_slug), record)
    if checkpoint_path is None:
        error_path = _error_path(provider_slug)
        if error_path.exists():
            error_path.unlink()
    return record


def _record_error(
    *,
    provider_slug: str,
    model: str,
    reasoning_effort: str,
    source_snapshot_sha256: str,
    exc: Exception,
) -> dict[str, Any]:
    record = {
        "schema_version": 1,
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": _prompt_sha256(),
        "source_snapshot_sha256": source_snapshot_sha256,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "provider_slug": provider_slug,
        "failed_at": now_iso(),
        "error_type": type(exc).__name__,
        "status_code": getattr(exc, "status_code", None),
        "error": str(exc),
    }
    _atomic_json(_error_path(provider_slug), record)
    return record


def _load_results() -> list[dict[str, Any]]:
    return [
        json.loads(_provider_path(provider_slug).read_text())
        for provider_slug in PROVIDER_SLUGS
        if _provider_path(provider_slug).exists()
    ]


def _write_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    results = _load_results()
    providers = []
    total_usage: Counter[str] = Counter()
    source_calls_succeeded = 0
    for result in results:
        source_calls_succeeded += len(result.get("response_ids") or []) or int(
            bool(result.get("response_id"))
        )
        judgments = result["judgments"]
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            total_usage[key] += int((result.get("usage") or {}).get(key) or 0)
        providers.append(
            {
                "provider_slug": result["provider_slug"],
                "cases_judged": len(judgments),
                "provider_values_present": sum(j["provider_present"] for j in judgments),
                "matches": sum(j["is_correct"] for j in judgments),
                "same_family_matches": sum(
                    j["relationship"] == "same_family" for j in judgments
                ),
                "changed_from_general_llm": sum(
                    j["changed_from_general_llm"] for j in judgments
                ),
                "changed_from_deterministic": sum(
                    j["changed_from_deterministic"] for j in judgments
                ),
                "mean_confidence": round(mean(j["confidence"] for j in judgments), 2),
                "latency_ms": result["latency_ms"],
                "usage": result.get("usage") or {},
            }
        )
    summary = {
        "schema_version": 1,
        "updated_at": now_iso(),
        "model": manifest["model"],
        "reasoning_effort": manifest["reasoning_effort"],
        "provider_batches_total": len(PROVIDER_SLUGS),
        "provider_batches_judged": len(results),
        "provider_batches_failed": sum(
            _error_path(provider_slug).exists() for provider_slug in PROVIDER_SLUGS
        ),
        "source_calls_succeeded": source_calls_succeeded,
        "industry_judgments": sum(len(result["judgments"]) for result in results),
        "changed_from_general_llm": sum(
            j["changed_from_general_llm"]
            for result in results
            for j in result["judgments"]
        ),
        "applied_policy_overrides": len(POLICY_OVERRIDES),
        "usage": dict(total_usage),
        "providers": sorted(providers, key=lambda item: item["provider_slug"]),
    }
    _atomic_json(SUMMARY_PATH, summary)
    return summary


def _replace_or_append_metric(
    metrics: list[dict[str, Any]], name: str, value: float, detail: dict[str, Any]
) -> None:
    metrics[:] = [metric for metric in metrics if metric["metric_name"] != name]
    metrics.append({"metric_name": name, "metric_value": value, "detail": detail})


def _apply(snapshot: dict[str, Any], manifest: dict[str, Any]) -> None:
    results = _load_results()
    if len(results) != len(PROVIDER_SLUGS):
        raise RuntimeError(f"cannot apply incomplete results: {len(results)}/{len(PROVIDER_SLUGS)}")
    result_by_provider = {result["provider_slug"]: result for result in results}
    judgments = {
        (result["provider_slug"], judgment["case_slug"]): judgment
        for result in results
        for judgment in result["judgments"]
    }

    for run in snapshot["runs"]:
        key = (run["provider_slug"], run["case_slug"])
        judgment = judgments.get(key)
        if judgment is None:
            continue
        policy_override = POLICY_OVERRIDES.get(key)
        effective_is_correct = (
            policy_override["is_correct"] if policy_override else judgment["is_correct"]
        )
        effective_relationship = (
            policy_override["relationship"]
            if policy_override
            else judgment["relationship"]
        )
        effective_rationale = (
            policy_override["rationale"] if policy_override else judgment["rationale"]
        )
        metrics = run.get("metrics") or []
        existing = _metric_map(run)
        previous_industry = existing.get("reference_correct_industry")
        if (
            previous_industry
            and "general_llm_reference_correct_industry" not in existing
        ):
            metrics.append(
                {
                    **previous_industry,
                    "metric_name": "general_llm_reference_correct_industry",
                }
            )
        for aggregate_name in (
            "reference_accuracy_when_present_pct",
            "correct_field_yield_pct",
        ):
            previous = existing.get(aggregate_name)
            preserved_name = f"general_llm_{aggregate_name}"
            if previous and preserved_name not in existing:
                metrics.append({**previous, "metric_name": preserved_name})

        result = result_by_provider[run["provider_slug"]]
        detail = {
            "reference_status": LINKEDIN_REFERENCE_STATUS,
            "judge_method": "openai_structured_industry_llm",
            "judge_model": result["model"],
            "judge_reasoning_effort": result["reasoning_effort"],
            "judge_prompt_version": result["prompt_version"],
            "relationship": effective_relationship,
            "confidence": judgment["confidence"],
            "rationale": effective_rationale,
            "raw_llm_is_correct": judgment["is_correct"],
            "policy_override": bool(policy_override),
        }
        _replace_or_append_metric(
            metrics,
            "reference_correct_industry",
            1 if effective_is_correct else 0,
            detail,
        )

        current = {metric["metric_name"]: metric for metric in metrics}
        evaluable_attributes = [
            attribute
            for attribute in SCORED_ATTRIBUTES
            if f"reference_correct_{attribute}" in current
        ]
        correct = sum(
            float(current[f"reference_correct_{attribute}"]["metric_value"])
            for attribute in evaluable_attributes
        )
        returned = sum(
            float(current.get(f"coverage_{attribute}", {}).get("metric_value") or 0)
            for attribute in evaluable_attributes
        )
        aggregate_detail = {
            "reference_status": LINKEDIN_REFERENCE_STATUS,
            "judge_method": "openai_structured_field_judges",
            "general_judge_model": snapshot.get("judge_model"),
            "industry_judge_model": manifest["model"],
            "industry_judge_prompt_version": manifest["prompt_version"],
            "truth_fields_evaluable": len(evaluable_attributes),
            "truth_fields_returned": int(returned),
        }
        _replace_or_append_metric(
            metrics,
            "correct_field_yield_pct",
            round(100 * correct / len(evaluable_attributes), 2),
            aggregate_detail,
        )
        if returned:
            _replace_or_append_metric(
                metrics,
                "reference_accuracy_when_present_pct",
                round(100 * correct / returned, 2),
                aggregate_detail,
            )
        else:
            metrics[:] = [
                metric
                for metric in metrics
                if metric["metric_name"] != "reference_accuracy_when_present_pct"
            ]
        run["metrics"] = metrics

    snapshot["evaluation_method"] = "openai_structured_field_judges"
    snapshot["industry_judge_model"] = manifest["model"]
    snapshot["industry_judge_reasoning_effort"] = manifest["reasoning_effort"]
    snapshot["industry_judge_prompt_version"] = manifest["prompt_version"]
    snapshot["industry_judge_manifest"] = str(MANIFEST_PATH.relative_to(ROOT))
    snapshot["industry_judge_summary"] = str(SUMMARY_PATH.relative_to(ROOT))
    snapshot["industry_judge_source_snapshot_sha256"] = manifest[
        "source_snapshot_sha256"
    ]
    snapshot["leaderboard"] = recompute_leaderboard(snapshot["runs"], snapshot["case_count"])
    snapshot["updated_at"] = now_iso()
    write_snapshot(snapshot)


def _csv(value: str | None) -> list[str]:
    return [item.strip().lower() for item in value.split(",") if item.strip()] if value else []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--confirm-paid", action="store_true")
    parser.add_argument("--only", help="comma-separated provider slugs")
    parser.add_argument("--workers", type=int, default=7)
    parser.add_argument("--retry-errors", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.workers < 1 or args.workers > 7:
        parser.error("--workers must be between 1 and 7")
    if not args.dry_run and not args.apply and not args.confirm_paid:
        parser.error("live judge calls require --confirm-paid")

    selected = _csv(args.only) or list(PROVIDER_SLUGS)
    unknown = sorted(set(selected) - set(PROVIDER_SLUGS))
    if unknown:
        parser.error(f"unknown provider(s): {', '.join(unknown)}")

    load_dotenv(ROOT / ".env.local", override=False)
    model = os.environ.get("OPENAI_JUDGE_MODEL", DEFAULT_MODEL)
    reasoning_effort = os.environ.get(
        "OPENAI_JUDGE_REASONING_EFFORT", DEFAULT_REASONING_EFFORT
    )
    snapshot = read_snapshot()
    if snapshot.get("case_count") != 300 or len(snapshot.get("runs") or []) != 2100:
        raise RuntimeError("industry judge requires the frozen 300-case, 2100-cell snapshot")

    existing_manifest = (
        json.loads(MANIFEST_PATH.read_text()) if MANIFEST_PATH.exists() else None
    )
    expected_manifest_path = str(MANIFEST_PATH.relative_to(ROOT))
    source_snapshot_sha256 = snapshot.get("industry_judge_source_snapshot_sha256")
    if (
        not source_snapshot_sha256
        and snapshot.get("industry_judge_manifest") == expected_manifest_path
        and existing_manifest
    ):
        source_snapshot_sha256 = existing_manifest["source_snapshot_sha256"]
    source_snapshot_sha256 = source_snapshot_sha256 or _file_sha256(SNAPSHOT_PATH)

    manifest = {
        "schema_version": 1,
        "created_at": now_iso(),
        "status": "running",
        "source_snapshot": str(SNAPSHOT_PATH.relative_to(ROOT)),
        "source_snapshot_sha256": source_snapshot_sha256,
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": _prompt_sha256(),
        "model": model,
        "reasoning_effort": reasoning_effort,
        "case_count": 300,
        "provider_count": len(PROVIDER_SLUGS),
        "provider_slugs": list(PROVIDER_SLUGS),
        "paid_call_design": "one all-industry structured-output call per provider",
    }
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if existing_manifest:
        immutable = (
            "source_snapshot_sha256",
            "prompt_version",
            "prompt_sha256",
            "model",
            "reasoning_effort",
            "provider_slugs",
        )
        if any(existing_manifest.get(key) != manifest.get(key) for key in immutable):
            raise RuntimeError("existing industry-judge manifest differs from current configuration")
        manifest = existing_manifest
    else:
        _atomic_json(MANIFEST_PATH, manifest)

    if args.apply:
        _apply(snapshot, manifest)
        manifest["status"] = "applied"
        manifest["applied_at"] = now_iso()
        _atomic_json(MANIFEST_PATH, manifest)
        print("applied dedicated industry judgments; paid calls=0")
        return 0

    pending = []
    skipped_success = 0
    skipped_error = 0
    for provider_slug in selected:
        if _provider_path(provider_slug).exists():
            skipped_success += 1
        elif _error_path(provider_slug).exists() and not args.retry_errors:
            skipped_error += 1
        else:
            pending.append(provider_slug)
    print(
        json.dumps(
            {
                "model": model,
                "reasoning_effort": reasoning_effort,
                "selected_providers": selected,
                "pending_paid_calls": len(pending),
                "industry_cases_per_call": 299,
                "skipped_success": skipped_success,
                "skipped_error": skipped_error,
            }
        ),
        flush=True,
    )
    if args.dry_run:
        _write_summary(manifest)
        return 0

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key or not api_key.startswith("sk-"):
        raise RuntimeError("OPENAI_API_KEY is not configured")
    client = OpenAI(api_key=api_key, max_retries=0, timeout=900.0)
    stop = threading.Event()
    failures = 0
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(args.workers, len(pending) or 1)
    ) as pool:
        futures = {
            pool.submit(
                _judge_provider,
                client,
                snapshot=snapshot,
                provider_slug=provider_slug,
                model=model,
                reasoning_effort=reasoning_effort,
                source_snapshot_sha256=source_snapshot_sha256,
            ): provider_slug
            for provider_slug in pending
        }
        for future in concurrent.futures.as_completed(futures):
            provider_slug = futures[future]
            try:
                record = future.result()
                print(
                    json.dumps(
                        {
                            "provider": provider_slug,
                            "status": "ok",
                            "cases": record["case_count"],
                            "latency_ms": record["latency_ms"],
                            "usage": record["usage"],
                        }
                    ),
                    flush=True,
                )
            except Exception as exc:  # noqa: BLE001 - checkpoint exact provider failure
                failures += 1
                record = _record_error(
                    provider_slug=provider_slug,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    source_snapshot_sha256=source_snapshot_sha256,
                    exc=exc,
                )
                print(
                    json.dumps(
                        {
                            "provider": provider_slug,
                            "status": "error",
                            "error_type": record["error_type"],
                            "status_code": record["status_code"],
                        }
                    ),
                    flush=True,
                )
                if record.get("status_code") in {401, 403, 429}:
                    stop.set()
                    for other in futures:
                        other.cancel()
            if stop.is_set():
                break

    summary = _write_summary(manifest)
    if summary["provider_batches_judged"] == len(PROVIDER_SLUGS) and not summary[
        "provider_batches_failed"
    ]:
        manifest["status"] = "complete"
        manifest["completed_at"] = now_iso()
        _atomic_json(MANIFEST_PATH, manifest)
    print(json.dumps(summary), flush=True)
    return 0 if not failures else 4


if __name__ == "__main__":
    raise SystemExit(main())
