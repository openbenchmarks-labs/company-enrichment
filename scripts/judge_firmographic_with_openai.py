"""Judge firmographic correctness in resumable OpenAI provider chunks.

By default each paid request contains 50 companies for exactly one provider.
Only attributes with non-null ground truth are included. Every chunk is
checkpointed independently, so resuming never repays for a completed call
unless ``--retry-errors`` is explicitly supplied.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean
from typing import Any, Literal

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field

from firmographic.common import (
    SCORED_ATTRIBUTES,
    SNAPSHOT_PATH,
    now_iso,
    read_snapshot,
    recompute_leaderboard,
    write_snapshot,
)


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "data" / "firmographic" / "llm-judge-v3"
LEGACY_OUTPUT_DIR = ROOT / "data" / "firmographic" / "llm-judge-v1"
PROVIDER_DIR = OUTPUT_DIR / "providers"
CHUNK_DIR = OUTPUT_DIR / "chunks"
ERROR_DIR = OUTPUT_DIR / "chunk-errors"
MANIFEST_PATH = OUTPUT_DIR / "manifest.json"
SUMMARY_PATH = OUTPUT_DIR / "summary.json"
PROMPT_VERSION = "firmographic-provider-chunk-judge-v3.0"
DEFAULT_MODEL = "gpt-5.6"
DEFAULT_REASONING_EFFORT = "high"
DEFAULT_BATCH_SIZE = 50
PROVIDER_SLUGS = (
    "apollo",
    "company-enrich",
    "explorium",
    "fiber",
    "ocean",
    "people-data-labs",
    "predictleads",
)

SYSTEM_PROMPT = """You are the independent field-level judge for a company-enrichment benchmark.

The input contains one checkpointed subset of the 300 benchmark companies for one provider. Each field includes an
authoritative reference value and the provider value. The reference is authoritative for this
task. Judge every supplied field. Compare meaning rather than superficial formatting, but remain
conservative about company identity and do not add outside facts.

Return provider_present=false and is_correct=false when no usable provider value was supplied.
Otherwise return provider_present=true and decide correctness using these rules:

- legal_name: Accept punctuation, capitalization, word-order, transliteration, common abbreviation,
  and legal-suffix differences when company identity is clearly the same. Reject a parent,
  subsidiary, similarly named entity, or materially different name.
- primary_domain: Ignore scheme, www, path, query, case, and trailing dot. Match only the same
  registrable company domain or an explicitly supplied equivalent official domain. The benchmark's
  audited official equivalents are: romanosigns.co.za=romano.co.za,
  gettopvote.com=downtimedollars.com, bergenmek.no=bmg-as.no,
  randstadinnovationfund.com=randstad.com, azerty.com.mx=azerty.mx,
  salesianibologna.it=salesianibologna.net, semirbiz.com=semir.com, and
  highpressurehose.com=thehoseguys.net. Do not treat a parent/subsidiary domain as equivalent
  without evidence in the input.
- hq_location: Compare only reference components supplied. Accept country names/codes,
  conventional city aliases, accents, and transliterations. When both country and city are
  supplied, both must agree.
- founded_year: Require the exact four-digit year.
- industry: Accept established taxonomy synonyms and a reasonably equivalent description of the
  same primary business. Reject adjacent sectors and labels so broad they lose material meaning.
- linkedin_url: Ignore scheme, host prefix, case, query, and trailing slash. Require the same
  company page identity. The audited redirect freshdelmonte=delmontecorporation is the same page;
  do not infer any other undocumented redirect.
- headcount_band: The reference includes its canonical LinkedIn band and, when available, an
  exact LinkedIn employee count. A provider band passes only when it is the same canonical
  LinkedIn band; `1-10` is equivalent to LinkedIn's `2-10` small-company band, but reject other
  merely overlapping broad bands. A provider exact count (min equals max)
  also passes when it is within plus or minus 5% of the reference exact count, even if that count
  falls on the other side of a band boundary. Do not use the exact-count tolerance for a provider
  range.

Output exactly one case result for every input case and exactly one field result for every supplied
field. Use a short, value-specific rationale. Do not omit rows, merge companies, or override the
reference with model memory."""


AttributeName = Literal[
    "legal_name",
    "primary_domain",
    "hq_location",
    "founded_year",
    "industry",
    "linkedin_url",
    "headcount_band",
]


class FieldJudgment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attribute: AttributeName
    provider_present: bool
    is_correct: bool
    confidence: int = Field(ge=0, le=100)
    rationale: str = Field(min_length=1, max_length=160)


class CaseJudgment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_slug: str
    fields: list[FieldJudgment]


class ProviderBatchOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_slug: str
    cases: list[CaseJudgment]


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _prompt_hash() -> str:
    return hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()


def _provider_path(provider_slug: str) -> Path:
    return PROVIDER_DIR / f"{provider_slug}.json"


def _chunk_path(provider_slug: str, chunk_index: int) -> Path:
    return CHUNK_DIR / provider_slug / f"chunk-{chunk_index:02d}.json"


def _error_path(provider_slug: str, chunk_index: int) -> Path:
    return ERROR_DIR / provider_slug / f"chunk-{chunk_index:02d}.json"


def _chunk_specs(snapshot: dict[str, Any], batch_size: int) -> list[dict[str, Any]]:
    case_slugs = [case["case_slug"] for case in snapshot["cases"]]
    return [
        {
            "chunk_index": start // batch_size,
            "case_slugs": case_slugs[start : start + batch_size],
        }
        for start in range(0, len(case_slugs), batch_size)
    ]


def _metric_map(run: dict[str, Any]) -> dict[str, float]:
    return {
        metric["metric_name"]: metric.get("metric_value")
        for metric in run.get("metrics") or []
        if metric.get("metric_value") is not None
    }


def _truth_available(reference: dict[str, Any], attribute: str) -> bool:
    if attribute == "hq_location":
        return bool(reference.get("hq_country") or reference.get("hq_city"))
    if attribute == "industry":
        return bool(reference.get("industry") or reference.get("industries"))
    if attribute == "headcount_band":
        return reference.get("headcount_min") is not None or reference.get("headcount_max") is not None
    return reference.get(attribute) not in (None, "", [])


def _field_value(company: dict[str, Any] | None, attribute: str) -> Any:
    if not company:
        return None
    if attribute == "legal_name":
        return company.get("legal_name")
    if attribute == "primary_domain":
        values = [company.get("primary_domain"), *(company.get("domains") or [])]
        values = list(dict.fromkeys(value for value in values if value))
        return values or None
    if attribute == "hq_location":
        value = {"country": company.get("hq_country"), "city": company.get("hq_city")}
        return value if any(value.values()) else None
    if attribute == "founded_year":
        return company.get("founded_year")
    if attribute == "industry":
        values = [company.get("industry"), *(company.get("industries") or [])]
        values = list(dict.fromkeys(value for value in values if value))
        return values or None
    if attribute == "linkedin_url":
        return company.get("linkedin_url")
    if attribute == "headcount_band":
        minimum = company.get("headcount_min")
        maximum = company.get("headcount_max")
        if minimum is None and maximum is None:
            return None
        value = {"min": minimum, "max": maximum}
        if company.get("headcount_exact") is not None:
            value["exact_count"] = company["headcount_exact"]
        return value
    raise KeyError(attribute)


def _build_provider_input(
    snapshot: dict[str, Any],
    provider_slug: str,
    *,
    selected_case_slugs: list[str] | None = None,
) -> tuple[dict[str, Any], dict[str, set[str]], dict[tuple[str, str], dict[str, Any]]]:
    cases = {case["case_slug"]: case for case in snapshot["cases"]}
    runs = {
        run["case_slug"]: run
        for run in snapshot["runs"]
        if run["provider_slug"] == provider_slug
    }
    if set(runs) != set(cases):
        raise ValueError(
            f"{provider_slug} has {len(runs)} runs for {len(cases)} benchmark cases"
        )

    selected = set(selected_case_slugs) if selected_case_slugs is not None else set(cases)
    if not selected <= set(cases):
        raise ValueError(f"unknown case slug(s): {sorted(selected - set(cases))[:5]}")

    expected: dict[str, set[str]] = {}
    source_fields: dict[tuple[str, str], dict[str, Any]] = {}
    rows = []
    for case in snapshot["cases"]:
        case_slug = case["case_slug"]
        if case_slug not in selected:
            continue
        reference = case.get("reference") or {}
        run = runs[case_slug]
        metrics = _metric_map(run)
        attributes = [
            attribute
            for attribute in SCORED_ATTRIBUTES
            if _truth_available(reference, attribute)
            and not (
                attribute == "primary_domain"
                and (case.get("source_metadata") or {}).get("input_domain_status") == "inactive"
            )
        ]
        expected[case_slug] = set(attributes)
        fields = []
        for attribute in attributes:
            reference_value = _field_value(reference, attribute)
            provider_value = _field_value(run.get("normalized"), attribute)
            if metrics.get(f"coverage_{attribute}") == 0:
                provider_value = None
            deterministic = (
                "not_returned"
                if provider_value is None
                else "match"
                if metrics.get(f"reference_correct_{attribute}") == 1
                else "mismatch"
            )
            source_fields[(case_slug, attribute)] = {
                "reference": reference_value,
                "provider_value": provider_value,
                "deterministic_baseline": deterministic,
            }
            fields.append(
                {
                    "attribute": attribute,
                    "reference": reference_value,
                    "provider_value": provider_value,
                }
            )
        rows.append(
            {
                "case_slug": case_slug,
                "input_domain": case["input_domain"],
                "response_status": run.get("status"),
                "fields": fields,
            }
        )
    return {"provider_slug": provider_slug, "cases": rows}, expected, source_fields


def _validate_output(
    output: ProviderBatchOutput,
    *,
    provider_slug: str,
    expected: dict[str, set[str]],
    source_fields: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    if output.provider_slug != provider_slug:
        raise ValueError(f"judge returned provider {output.provider_slug}, expected {provider_slug}")
    returned_cases: dict[str, CaseJudgment] = {}
    for case in output.cases:
        if case.case_slug in returned_cases:
            raise ValueError(f"judge duplicated case {case.case_slug}")
        returned_cases[case.case_slug] = case
    if set(returned_cases) != set(expected):
        missing = sorted(set(expected) - set(returned_cases))
        extra = sorted(set(returned_cases) - set(expected))
        raise ValueError(f"judge case-set mismatch: missing={missing[:5]}, extra={extra[:5]}")

    validated = []
    for case_slug in expected:
        field_rows: dict[str, FieldJudgment] = {}
        for field in returned_cases[case_slug].fields:
            if field.attribute in field_rows:
                raise ValueError(f"judge duplicated {case_slug}/{field.attribute}")
            field_rows[field.attribute] = field
        if set(field_rows) != expected[case_slug]:
            raise ValueError(
                f"judge field-set mismatch for {case_slug}: "
                f"got={sorted(field_rows)}, expected={sorted(expected[case_slug])}"
            )

        fields = []
        for attribute in sorted(field_rows):
            judgment = field_rows[attribute]
            source = source_fields[(case_slug, attribute)]
            actually_present = source["provider_value"] is not None
            if judgment.provider_present != actually_present:
                raise ValueError(
                    f"judge presence mismatch for {case_slug}/{attribute}: "
                    f"judge={judgment.provider_present}, actual={actually_present}"
                )
            if not actually_present and judgment.is_correct:
                raise ValueError(f"judge marked absent value correct: {case_slug}/{attribute}")
            verdict = (
                "not_returned"
                if not judgment.provider_present
                else "match"
                if judgment.is_correct
                else "mismatch"
            )
            fields.append(
                {
                    **judgment.model_dump(),
                    "verdict": verdict,
                    "reference": source["reference"],
                    "provider_value": source["provider_value"],
                    "deterministic_baseline": source["deterministic_baseline"],
                    "changed_from_deterministic": verdict
                    != source["deterministic_baseline"],
                }
            )
        evaluable = len(fields)
        returned = sum(field["provider_present"] for field in fields)
        correct = sum(field["is_correct"] for field in fields)
        validated.append(
            {
                "case_slug": case_slug,
                "fields": fields,
                "evaluable_fields": evaluable,
                "returned_fields": returned,
                "correct_fields": correct,
                "correct_field_yield_pct": round(100 * correct / evaluable, 2),
                "accuracy_when_present_pct": (
                    round(100 * correct / returned, 2) if returned else None
                ),
            }
        )
    return validated


def _usage_dict(response: Any) -> dict[str, Any]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    return usage.model_dump(mode="json") if hasattr(usage, "model_dump") else dict(usage)


def _judge_chunk(
    client: OpenAI,
    *,
    snapshot: dict[str, Any],
    provider_slug: str,
    chunk_index: int,
    case_slugs: list[str],
    model: str,
    reasoning_effort: str,
    snapshot_sha256: str,
) -> dict[str, Any]:
    request_input, expected, source_fields = _build_provider_input(
        snapshot,
        provider_slug,
        selected_case_slugs=case_slugs,
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
        text_format=ProviderBatchOutput,
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
        source_fields=source_fields,
    )
    record = {
        "schema_version": 1,
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": _prompt_hash(),
        "snapshot_sha256": snapshot_sha256,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "provider_slug": provider_slug,
        "chunk_index": chunk_index,
        "case_slugs": case_slugs,
        "judged_at": now_iso(),
        "latency_ms": latency_ms,
        "response_id": response.id,
        "usage": _usage_dict(response),
        "case_count": len(judgments),
        "judgments": judgments,
    }
    _atomic_json(_chunk_path(provider_slug, chunk_index), record)
    error_path = _error_path(provider_slug, chunk_index)
    if error_path.exists():
        error_path.unlink()
    return record


def _aggregate_provider(
    *,
    snapshot: dict[str, Any],
    provider_slug: str,
    chunk_specs: list[dict[str, Any]],
    snapshot_sha256: str,
) -> dict[str, Any] | None:
    paths = [_chunk_path(provider_slug, spec["chunk_index"]) for spec in chunk_specs]
    if not all(path.exists() for path in paths):
        return None
    chunks = [json.loads(path.read_text()) for path in paths]
    expected_case_slugs = [case["case_slug"] for case in snapshot["cases"]]
    judgments_by_case = {
        judgment["case_slug"]: judgment
        for chunk in chunks
        for judgment in chunk["judgments"]
    }
    if set(judgments_by_case) != set(expected_case_slugs):
        raise ValueError(
            f"cannot aggregate {provider_slug}: got {len(judgments_by_case)}/"
            f"{len(expected_case_slugs)} cases"
        )
    usage: Counter[str] = Counter()
    for chunk in chunks:
        for key, value in (chunk.get("usage") or {}).items():
            if isinstance(value, (int, float)):
                usage[key] += value
    record = {
        "schema_version": 2,
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": _prompt_hash(),
        "snapshot_sha256": snapshot_sha256,
        "model": chunks[0]["model"],
        "reasoning_effort": chunks[0]["reasoning_effort"],
        "provider_slug": provider_slug,
        "judged_at": now_iso(),
        "latency_ms": sum(chunk["latency_ms"] for chunk in chunks),
        "wall_latency_ms": max(chunk["latency_ms"] for chunk in chunks),
        "response_ids": [chunk["response_id"] for chunk in chunks],
        "usage": dict(usage),
        "case_count": len(expected_case_slugs),
        "chunk_count": len(chunks),
        "judgments": [judgments_by_case[case_slug] for case_slug in expected_case_slugs],
    }
    _atomic_json(_provider_path(provider_slug), record)
    return record


def _import_legacy_provider(provider_slug: str, *, snapshot_sha256: str) -> bool:
    """Reuse an already-paid monolithic result without issuing duplicate calls."""
    destination = _provider_path(provider_slug)
    source = LEGACY_OUTPUT_DIR / "providers" / f"{provider_slug}.json"
    if destination.exists() or not source.exists():
        return destination.exists()
    record = json.loads(source.read_text())
    if record.get("snapshot_sha256") != snapshot_sha256 or record.get("case_count") != 300:
        return False
    record["imported_from"] = str(source.relative_to(ROOT))
    record["legacy_monolithic_batch"] = True
    _atomic_json(destination, record)
    return True


def _error_record(
    *,
    provider_slug: str,
    chunk_index: int,
    case_slugs: list[str],
    model: str,
    reasoning_effort: str,
    snapshot_sha256: str,
    exc: Exception,
) -> dict[str, Any]:
    record = {
        "schema_version": 1,
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": _prompt_hash(),
        "snapshot_sha256": snapshot_sha256,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "provider_slug": provider_slug,
        "chunk_index": chunk_index,
        "case_slugs": case_slugs,
        "failed_at": now_iso(),
        "error_type": type(exc).__name__,
        "status_code": getattr(exc, "status_code", None),
        "error": str(exc),
    }
    _atomic_json(_error_path(provider_slug, chunk_index), record)
    return record


def _build_manifest(
    snapshot: dict[str, Any],
    *,
    model: str,
    reasoning_effort: str,
    snapshot_sha256: str,
    batch_size: int,
) -> dict[str, Any]:
    chunk_count = len(_chunk_specs(snapshot, batch_size))
    return {
        "schema_version": 1,
        "created_at": now_iso(),
        "status": "running",
        "source_snapshot": str(SNAPSHOT_PATH.relative_to(ROOT)),
        "snapshot_sha256": snapshot_sha256,
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": _prompt_hash(),
        "model": model,
        "reasoning_effort": reasoning_effort,
        "case_count": len(snapshot["cases"]),
        "provider_count": len(PROVIDER_SLUGS),
        "batch_size": batch_size,
        "chunks_per_provider": chunk_count,
        "paid_calls_total": len(PROVIDER_SLUGS) * chunk_count,
        "paid_call_design": "checkpointed fixed-size company chunks, one provider per call",
        "active_attributes": list(SCORED_ATTRIBUTES),
        "provider_slugs": list(PROVIDER_SLUGS),
        "case_slugs": [case["case_slug"] for case in snapshot["cases"]],
    }


def _load_results(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        json.loads(_provider_path(slug).read_text())
        for slug in manifest["provider_slugs"]
        if _provider_path(slug).exists()
    ]


def _write_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    results = _load_results(manifest)
    chunk_indexes = range(manifest["chunks_per_provider"])
    chunk_successes = sum(
        _chunk_path(slug, index).exists()
        for slug in manifest["provider_slugs"]
        for index in chunk_indexes
    )
    chunk_errors = sum(
        _error_path(slug, index).exists()
        for slug in manifest["provider_slugs"]
        for index in chunk_indexes
    )
    providers_with_errors = sum(
        not _provider_path(slug).exists()
        and any(_error_path(slug, index).exists() for index in chunk_indexes)
        for slug in manifest["provider_slugs"]
    )
    usage: Counter[str] = Counter()
    providers = []
    total_fields = 0
    total_changed = 0
    judged_by_case: Counter[str] = Counter()
    for result in results:
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            usage[key] += int((result.get("usage") or {}).get(key) or 0)
        cells = result["judgments"]
        for cell in cells:
            judged_by_case[cell["case_slug"]] += 1
        fields = [field for cell in cells for field in cell["fields"]]
        total_fields += len(fields)
        changed = sum(field["changed_from_deterministic"] for field in fields)
        total_changed += changed
        yields = [cell["correct_field_yield_pct"] for cell in cells]
        accuracies = [
            cell["accuracy_when_present_pct"]
            for cell in cells
            if cell["accuracy_when_present_pct"] is not None
        ]
        providers.append(
            {
                "provider_slug": result["provider_slug"],
                "cases_judged": len(cells),
                "field_judgments": len(fields),
                "matches": sum(field["is_correct"] for field in fields),
                "changed_from_deterministic": changed,
                "avg_correct_field_yield_pct": round(mean(yields), 2),
                "avg_accuracy_when_present_pct": round(mean(accuracies), 2),
                "latency_ms": result["latency_ms"],
                "usage": result.get("usage") or {},
            }
        )
    summary = {
        "schema_version": 1,
        "updated_at": now_iso(),
        "model": manifest["model"],
        "reasoning_effort": manifest["reasoning_effort"],
        "provider_batches_total": manifest["provider_count"],
        "provider_batches_judged": len(results),
        "provider_batches_failed": providers_with_errors,
        "provider_batches_pending": manifest["provider_count"] - len(results),
        "chunk_calls_total": manifest["paid_calls_total"],
        "chunk_calls_succeeded": chunk_successes,
        "chunk_calls_failed": chunk_errors,
        "legacy_monolithic_results_reused": sum(
            bool(result.get("legacy_monolithic_batch")) for result in results
        ),
        "cases_fully_judged": sum(
            count == manifest["provider_count"] for count in judged_by_case.values()
        ),
        "cells_judged": sum(len(result["judgments"]) for result in results),
        "field_judgments": total_fields,
        "changed_from_deterministic": total_changed,
        "changed_from_deterministic_pct": (
            round(100 * total_changed / total_fields, 2) if total_fields else None
        ),
        "usage": dict(usage),
        "providers": sorted(providers, key=lambda row: row["provider_slug"]),
    }
    _atomic_json(SUMMARY_PATH, summary)
    return summary


def _apply_judgments(snapshot: dict[str, Any], manifest: dict[str, Any]) -> None:
    results = _load_results(manifest)
    if len(results) != manifest["provider_count"]:
        raise RuntimeError(
            f"cannot apply incomplete judge set: {len(results)}/{manifest['provider_count']} providers"
        )
    judgments = {
        (cell["case_slug"], result["provider_slug"]): cell
        for result in results
        for cell in result["judgments"]
    }
    result_by_provider = {result["provider_slug"]: result for result in results}
    expected_cells = manifest["case_count"] * manifest["provider_count"]
    if len(judgments) != expected_cells:
        raise RuntimeError(f"cannot apply: found {len(judgments)}/{expected_cells} judged cells")

    for run in snapshot["runs"]:
        cell = judgments[(run["case_slug"], run["provider_slug"])]
        provider_result = result_by_provider[run["provider_slug"]]
        metrics = run.get("metrics") or []
        existing_names = {metric["metric_name"] for metric in metrics}
        preserved = []
        for metric in metrics:
            name = metric["metric_name"]
            if name in {"reference_accuracy_when_present_pct", "correct_field_yield_pct"} or name.startswith(
                "reference_correct_"
            ):
                deterministic_name = f"deterministic_{name}"
                if deterministic_name not in existing_names:
                    preserved.append({**metric, "metric_name": deterministic_name})
                continue
            preserved.append(metric)

        detail_base = {
            "reference_status": "linkedin_live_ground_truth_v1",
            "judge_method": "openai_structured_llm",
            "judge_model": provider_result["model"],
            "judge_reasoning_effort": provider_result["reasoning_effort"],
            "judge_prompt_version": provider_result["prompt_version"],
            "judge_batching": (
                "legacy_300_company_batch"
                if provider_result.get("legacy_monolithic_batch")
                else f"{manifest['batch_size']}_company_chunks"
            ),
        }
        for field in cell["fields"]:
            preserved.append(
                {
                    "metric_name": f"reference_correct_{field['attribute']}",
                    "metric_value": 1 if field["is_correct"] else 0,
                    "detail": {
                        **detail_base,
                        "verdict": field["verdict"],
                        "confidence": field["confidence"],
                        "rationale": field["rationale"],
                    },
                }
            )
        if cell["accuracy_when_present_pct"] is not None:
            preserved.append(
                {
                    "metric_name": "reference_accuracy_when_present_pct",
                    "metric_value": cell["accuracy_when_present_pct"],
                    "detail": {
                        **detail_base,
                        "truth_fields_evaluable": cell["evaluable_fields"],
                        "truth_fields_returned": cell["returned_fields"],
                    },
                }
            )
        preserved.append(
            {
                "metric_name": "correct_field_yield_pct",
                "metric_value": cell["correct_field_yield_pct"],
                "detail": {
                    **detail_base,
                    "truth_fields_evaluable": cell["evaluable_fields"],
                    "truth_fields_returned": cell["returned_fields"],
                },
            }
        )
        run["metrics"] = preserved

    snapshot["evaluation_method"] = "openai_structured_llm_judge"
    snapshot["judge_model"] = manifest["model"]
    snapshot["judge_reasoning_effort"] = manifest["reasoning_effort"]
    prompt_versions = sorted({result["prompt_version"] for result in results})
    snapshot["judge_prompt_version"] = (
        prompt_versions[0] if len(prompt_versions) == 1 else "mixed_by_provider"
    )
    snapshot["judge_prompt_versions"] = prompt_versions
    snapshot["judge_batch_size"] = manifest["batch_size"]
    snapshot["judge_source_snapshot_sha256"] = manifest["snapshot_sha256"]
    snapshot["judge_manifest"] = str(MANIFEST_PATH.relative_to(ROOT))
    snapshot["judge_summary"] = str(SUMMARY_PATH.relative_to(ROOT))
    snapshot["leaderboard"] = recompute_leaderboard(snapshot["runs"], snapshot["case_count"])
    snapshot["updated_at"] = now_iso()
    write_snapshot(snapshot)


def _csv(value: str | None) -> list[str]:
    return [item.strip().lower() for item in value.split(",") if item.strip()] if value else []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--confirm-paid", action="store_true")
    parser.add_argument("--only", help="comma-separated providers")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--retry-errors", action="store_true")
    parser.add_argument(
        "--ignore-legacy",
        action="store_true",
        help="do not import a legacy monolithic provider result; use for an explicit rejudge",
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.workers < 1 or args.workers > 42:
        parser.error("--workers must be between 1 and 42")
    if args.batch_size < 1 or args.batch_size > 300:
        parser.error("--batch-size must be between 1 and 300")
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
        raise RuntimeError("judge requires the frozen 300-case, 2100-cell snapshot")
    existing_manifest = (
        json.loads(MANIFEST_PATH.read_text()) if MANIFEST_PATH.exists() else None
    )
    expected_manifest_path = str(MANIFEST_PATH.relative_to(ROOT))
    snapshot_sha256 = snapshot.get("judge_source_snapshot_sha256")
    if (
        not snapshot_sha256
        and snapshot.get("judge_manifest") == expected_manifest_path
        and existing_manifest
    ):
        # Applying judgments changes the snapshot file's metrics but not the
        # frozen references/provider responses that were sent to the judge.
        snapshot_sha256 = existing_manifest["snapshot_sha256"]
    snapshot_sha256 = snapshot_sha256 or _sha256(SNAPSHOT_PATH)
    manifest = _build_manifest(
        snapshot,
        model=model,
        reasoning_effort=reasoning_effort,
        snapshot_sha256=snapshot_sha256,
        batch_size=args.batch_size,
    )
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if existing_manifest:
        existing = existing_manifest
        immutable_keys = (
            "snapshot_sha256",
            "prompt_version",
            "prompt_sha256",
            "model",
            "reasoning_effort",
            "batch_size",
            "provider_slugs",
            "case_slugs",
        )
        if any(existing.get(key) != manifest.get(key) for key in immutable_keys):
            raise RuntimeError("existing LLM-judge manifest differs from current configuration")
        manifest = existing
    else:
        _atomic_json(MANIFEST_PATH, manifest)

    if args.apply:
        _apply_judgments(snapshot, manifest)
        manifest["status"] = "applied"
        manifest["applied_at"] = now_iso()
        _atomic_json(MANIFEST_PATH, manifest)
        print("applied 2100 LLM-judged cells; vendor calls=0")
        return 0

    chunk_specs = _chunk_specs(snapshot, args.batch_size)
    if not args.ignore_legacy:
        for provider_slug in selected:
            _import_legacy_provider(provider_slug, snapshot_sha256=snapshot_sha256)

    pending: list[tuple[str, dict[str, Any]]] = []
    skipped_success = 0
    skipped_error = 0
    for provider_slug in selected:
        if _provider_path(provider_slug).exists():
            skipped_success += len(chunk_specs)
            continue
        for spec in chunk_specs:
            chunk_index = spec["chunk_index"]
            if _chunk_path(provider_slug, chunk_index).exists():
                skipped_success += 1
            elif _error_path(provider_slug, chunk_index).exists() and not args.retry_errors:
                skipped_error += 1
            else:
                pending.append((provider_slug, spec))
    print(
        json.dumps(
            {
                "model": model,
                "reasoning_effort": reasoning_effort,
                "selected_providers": selected,
                "pending_paid_calls": len(pending),
                "companies_per_call": args.batch_size,
                "chunks_per_provider": len(chunk_specs),
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
    successes = 0
    failures = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(args.workers, len(pending) or 1)) as pool:
        futures = {
            pool.submit(
                _judge_chunk,
                client,
                snapshot=snapshot,
                provider_slug=provider_slug,
                chunk_index=spec["chunk_index"],
                case_slugs=spec["case_slugs"],
                model=model,
                reasoning_effort=reasoning_effort,
                snapshot_sha256=snapshot_sha256,
            ): (provider_slug, spec)
            for provider_slug, spec in pending
        }
        for future in concurrent.futures.as_completed(futures):
            provider_slug, spec = futures[future]
            chunk_index = spec["chunk_index"]
            try:
                record = future.result()
                successes += 1
                _aggregate_provider(
                    snapshot=snapshot,
                    provider_slug=provider_slug,
                    chunk_specs=chunk_specs,
                    snapshot_sha256=snapshot_sha256,
                )
                print(
                    json.dumps(
                        {
                            "provider": provider_slug,
                            "chunk": chunk_index,
                            "status": "ok",
                            "cases": record["case_count"],
                            "latency_ms": record["latency_ms"],
                            "usage": record["usage"],
                        }
                    ),
                    flush=True,
                )
            except Exception as exc:  # noqa: BLE001 - checkpoint every failure
                failures += 1
                record = _error_record(
                    provider_slug=provider_slug,
                    chunk_index=chunk_index,
                    case_slugs=spec["case_slugs"],
                    model=model,
                    reasoning_effort=reasoning_effort,
                    snapshot_sha256=snapshot_sha256,
                    exc=exc,
                )
                print(
                    json.dumps(
                        {
                            "provider": provider_slug,
                            "chunk": chunk_index,
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
    if summary["provider_batches_judged"] == manifest["provider_count"] and not summary[
        "provider_batches_failed"
    ]:
        manifest["status"] = "complete"
        manifest["completed_at"] = now_iso()
        _atomic_json(MANIFEST_PATH, manifest)
    print(json.dumps(summary), flush=True)
    return 0 if not failures else 4


if __name__ == "__main__":
    raise SystemExit(main())
