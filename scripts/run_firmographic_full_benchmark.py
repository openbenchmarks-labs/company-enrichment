"""Credit-safe full-cohort company-enrichment runner.

The runner evaluates the frozen 300-company final cohort while preserving and
reusing every existing provider/company cell. It runs at most
one request chain per provider concurrently and serializes all checkpoint
writes in the main thread.

Existing cells of every status are skipped by default. Retrying an error or a
pending cell requires an explicit ``--retry-status`` selection so a restart
cannot silently spend credits again.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from _shared import load_environment
from firmographic.common import (
    DATASET_NAME,
    DATASET_SLUG,
    LINKEDIN_REFERENCE_STATUS,
    RUNS_DIR,
    SCORED_ATTRIBUTES,
    SNAPSHOT_PATH,
    CompanyCase,
    NormalizedCompany,
    ProviderResult,
    benchmark_metrics,
    load_all_cases,
    now_iso,
    read_snapshot,
    recompute_leaderboard,
    write_snapshot,
)
from firmographic.providers import REGISTRY
from firmographic.providers.base import ProviderHTTPError


DEFAULT_SOURCE_ENVS: tuple[Path, ...] = ()
CELL_DIR = RUNS_DIR / "cells-v1"
ATTEMPT_LOG = RUNS_DIR / "attempts-v1.jsonl"
TERMINAL_STATUSES = {"ok", "not_found"}
RETRYABLE_HTTP_STATUSES = {408, 425, 429, 500, 502, 503, 504}
STOP_HTTP_STATUSES = {401, 402, 403, 429}
MIN_START_INTERVAL_SECONDS = {
    # Ocean documents/enforces 60 requests per minute. Keep headroom around the
    # exact one-second boundary so network jitter does not trigger another 429.
    "ocean": 1.1,
}


def _csv(value: str | None) -> list[str]:
    return [item.strip().lower() for item in value.split(",") if item.strip()] if value else []


def _load_envs(extra_paths: list[str]) -> list[str]:
    load_environment()
    loaded: list[str] = []
    for path in [*DEFAULT_SOURCE_ENVS, *(Path(value).expanduser() for value in extra_paths)]:
        if path.exists():
            load_dotenv(path, override=False)
            loaded.append(str(path))
    return loaded


def _new_snapshot(cases: list[CompanyCase], manifest: dict[str, Any]) -> dict[str, Any]:
    reference_status = manifest.get("reference_status")
    if reference_status != LINKEDIN_REFERENCE_STATUS:
        raise ValueError("the runner requires the frozen human-verified ground truth")
    return {
        "schema_version": "1.1",
        "dataset_slug": DATASET_SLUG,
        "dataset_name": DATASET_NAME,
        "status": "running",
        "reference_status": reference_status,
        "methodology_note": (
            "Available fields recorded from 300 identity-verified LinkedIn company pages are "
            "used as ground truth. Missing reference fields are excluded from denominators."
        ),
        "slice": "all_current_slices",
        "case_count": len(cases),
        "scored_attributes": list(SCORED_ATTRIBUTES),
        "reference_source": manifest.get("reference_source"),
        "reference_file": manifest.get("reference_file"),
        "reference_sha256": manifest.get("reference_sha256"),
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "cases": [case.to_dict() for case in cases],
        "runs": [],
        "leaderboard": [],
    }


def _safe_cell_filename(case_slug: str, provider_slug: str) -> str:
    value = f"{case_slug}__{provider_slug}"
    if not re.fullmatch(r"[a-zA-Z0-9_.-]+", value):
        raise ValueError(f"unsafe checkpoint key: {value!r}")
    return value + ".json"


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def _write_cell(payload: dict[str, Any]) -> None:
    path = CELL_DIR / _safe_cell_filename(payload["case_slug"], payload["provider_slug"])
    _atomic_json(path, payload)


def _append_attempt(payload: dict[str, Any]) -> None:
    ATTEMPT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ATTEMPT_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _load_ledger() -> list[dict[str, Any]]:
    if not CELL_DIR.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(CELL_DIR.glob("*.json")):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"could not read cell checkpoint {path}: {exc}") from exc
        if not isinstance(row, dict):
            raise RuntimeError(f"cell checkpoint {path} is not an object")
        rows.append(row)
    return rows


def _merge_run(rows: list[dict[str, Any]], indexes: dict[tuple[str, str], int], payload: dict[str, Any]) -> None:
    key = (payload["case_slug"], payload["provider_slug"])
    existing_index = indexes.get(key)
    if existing_index is None:
        indexes[key] = len(rows)
        rows.append(payload)
    else:
        rows[existing_index] = payload


def _prepare_snapshot(
    cases: list[CompanyCase], manifest: dict[str, Any], *, mutate: bool
) -> tuple[dict[str, Any], dict[tuple[str, str], int]]:
    snapshot = _new_snapshot(cases, manifest)
    valid_cases = {case.case_slug: case for case in cases}
    indexes: dict[tuple[str, str], int] = {}

    if SNAPSHOT_PATH.exists():
        existing = read_snapshot()
        if existing.get("dataset_slug") != DATASET_SLUG:
            raise RuntimeError(
                f"existing snapshot dataset {existing.get('dataset_slug')!r} does not match {DATASET_SLUG!r}"
            )
        for old_case in existing.get("cases") or []:
            current = valid_cases.get(old_case.get("case_slug"))
            if current and current.input_domain != old_case.get("input_domain"):
                raise RuntimeError(
                    f"refusing resume: {current.case_slug} changed domain from "
                    f"{old_case.get('input_domain')} to {current.input_domain}"
                )
        snapshot["created_at"] = existing.get("created_at") or snapshot["created_at"]
        for row in existing.get("runs") or []:
            if row.get("case_slug") not in valid_cases or row.get("provider_slug") not in REGISTRY:
                continue
            key = (row["case_slug"], row["provider_slug"])
            if key in indexes:
                raise RuntimeError(f"duplicate existing run cell {key}")
            indexes[key] = len(snapshot["runs"])
            snapshot["runs"].append(row)

    for row in _load_ledger():
        if row.get("case_slug") not in valid_cases or row.get("provider_slug") not in REGISTRY:
            continue
        _merge_run(snapshot["runs"], indexes, row)

    # Checkpoint files preserve raw normalized outputs but may carry metrics
    # from an older reference. Recompute every cell locally without vendor calls.
    for row in snapshot["runs"]:
        case = valid_cases[row["case_slug"]]
        normalized = NormalizedCompany(**row["normalized"]) if row.get("normalized") else None
        result = ProviderResult(
            provider_slug=row["provider_slug"],
            provider_name=row.get("provider_name") or row["provider_slug"],
            case_slug=row["case_slug"],
            status=row.get("status") or "error",
            latency_ms=row.get("latency_ms") or 0,
            normalized=normalized,
            cost_units=row.get("cost_units"),
            cost_unit=row.get("cost_unit"),
            ambiguity_count=row.get("ambiguity_count") or 0,
            audit=row.get("audit") or {},
            error=row.get("error"),
            queried_at=row.get("queried_at") or now_iso(),
        )
        row["metrics"] = benchmark_metrics(case, result)

    snapshot["leaderboard"] = recompute_leaderboard(snapshot["runs"], len(cases))
    snapshot["updated_at"] = now_iso()
    if mutate:
        write_snapshot(snapshot)
    return snapshot, indexes


def _error_result(case: CompanyCase, module: Any, exc: Exception, wall_clock_ms: int) -> ProviderResult:
    status_code = getattr(exc, "status_code", None)
    retry_after = getattr(exc, "retry_after", None)
    elapsed_ms = getattr(exc, "elapsed_ms", None)
    if status_code == 429:
        error_kind = "rate_limited"
    elif status_code in {401, 403}:
        error_kind = "authentication"
    elif status_code == 402:
        error_kind = "quota_or_payment"
    elif isinstance(exc, requests.Timeout):
        error_kind = "timeout"
    elif isinstance(exc, requests.ConnectionError):
        error_kind = "connection"
    elif isinstance(exc, ProviderHTTPError):
        error_kind = "provider_http"
    else:
        error_kind = "adapter"
    retriable = bool(status_code in RETRYABLE_HTTP_STATUSES or error_kind in {"timeout", "connection"})
    return ProviderResult(
        provider_slug=module.VENDOR_SLUG,
        provider_name=module.VENDOR_NAME,
        case_slug=case.case_slug,
        status="error",
        latency_ms=int(elapsed_ms or wall_clock_ms),
        error=f"{type(exc).__name__}: {exc}",
        audit={
            "error_kind": error_kind,
            "http_status": status_code,
            "retry_after": retry_after,
            "retriable": retriable,
            "stop_provider": status_code in STOP_HTTP_STATUSES,
            "wall_clock_ms": wall_clock_ms,
            "automatic_retry_attempted": False,
        },
    )


def _call_provider(module: Any, case: CompanyCase) -> ProviderResult:
    started = time.perf_counter()
    try:
        result = module.run(case)
    except Exception as exc:  # noqa: BLE001 - failures become auditable result rows
        wall_clock_ms = round((time.perf_counter() - started) * 1000)
        return _error_result(case, module, exc, wall_clock_ms)
    wall_clock_ms = round((time.perf_counter() - started) * 1000)
    result.audit = dict(result.audit or {})
    result.audit["wall_clock_ms"] = wall_clock_ms
    result.audit["automatic_retry_attempted"] = False
    return result


def _call_provider_after_delay(module: Any, case: CompanyCase, delay_seconds: float) -> ProviderResult:
    if delay_seconds > 0:
        time.sleep(delay_seconds)
    result = _call_provider(module, case)
    result.audit = dict(result.audit or {})
    result.audit["throttle_delay_ms"] = round(delay_seconds * 1000)
    return result


def _checkpoint(snapshot: dict[str, Any]) -> None:
    snapshot["leaderboard"] = recompute_leaderboard(snapshot["runs"], snapshot["case_count"])
    snapshot["updated_at"] = now_iso()
    write_snapshot(snapshot)


def _maximum_requests(modules: list[Any], pending: dict[str, list[CompanyCase]]) -> int:
    return sum(module.PAID_CALLS_PER_CASE * len(pending[module.VENDOR_SLUG]) for module in modules)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", help="comma-separated provider slugs")
    parser.add_argument("--source-env", action="append", default=[], help="additional vendor env file")
    parser.add_argument("--dry-run", action="store_true", help="inventory only; no writes or API calls")
    parser.add_argument("--confirm-paid", action="store_true", help="acknowledge calls may consume credits")
    parser.add_argument(
        "--retry-status",
        help="explicitly rerun existing cells with these statuses, e.g. error,pending",
    )
    parser.add_argument("--checkpoint-every", type=int, default=25)
    args = parser.parse_args()

    if not args.dry_run and not args.confirm_paid:
        parser.error("live vendor calls require --confirm-paid")
    if args.checkpoint_every < 1:
        parser.error("--checkpoint-every must be at least 1")

    requested = _csv(args.only)
    retry_statuses = set(_csv(args.retry_status))
    unknown = sorted(set(requested) - set(REGISTRY))
    if unknown:
        parser.error(f"unknown provider(s): {', '.join(unknown)}")
    invalid_statuses = sorted(retry_statuses - {"error", "pending", "ok", "not_found"})
    if invalid_statuses:
        parser.error(f"invalid retry status(es): {', '.join(invalid_statuses)}")
    modules = [module for slug, module in REGISTRY.items() if not requested or slug in requested]

    loaded_envs = _load_envs(args.source_env)
    cases, manifest = load_all_cases()
    snapshot, indexes = _prepare_snapshot(cases, manifest, mutate=not args.dry_run)
    existing = {
        (row["case_slug"], row["provider_slug"]): row
        for row in snapshot.get("runs") or []
    }
    pending: dict[str, list[CompanyCase]] = {}
    for module in modules:
        provider_pending = []
        for case in cases:
            row = existing.get((case.case_slug, module.VENDOR_SLUG))
            if row is None or row.get("status") in retry_statuses:
                provider_pending.append(case)
        pending[module.VENDOR_SLUG] = provider_pending

    missing_env = {
        module.VENDOR_SLUG: [key for key in module.REQUIRED_ENV if not os.environ.get(key)]
        for module in modules
    }
    missing_env = {slug: keys for slug, keys in missing_env.items() if keys}
    if missing_env and not args.dry_run:
        print("missing env: " + "; ".join(f"{slug}={','.join(keys)}" for slug, keys in missing_env.items()))
        return 2

    print(f"cohort: {len(cases)} unique domains across 4 slices")
    print(f"existing cells protected: {len(existing)}; selected providers: {len(modules)}")
    for module in modules:
        print(
            f"  {module.VENDOR_SLUG}: skip={len(cases) - len(pending[module.VENDOR_SLUG])}, "
            f"call={len(pending[module.VENDOR_SLUG])}"
        )
    maximum_requests = _maximum_requests(modules, pending)
    print(f"maximum new endpoint requests: {maximum_requests}")
    print(f"vendor env files found: {len(loaded_envs)}; credentials are not printed")
    if missing_env:
        print("missing env for live run: " + "; ".join(f"{slug}={','.join(keys)}" for slug, keys in missing_env.items()))
    if args.dry_run:
        print("dry-run complete: 0 vendor calls, 0 snapshot writes")
        return 0

    active_modules: list[Any] = []
    stopped: dict[str, str] = {}
    for module in modules:
        if not pending[module.VENDOR_SLUG]:
            continue
        if hasattr(module, "preflight"):
            try:
                info = module.preflight()
                safe_info = ", ".join(f"{key}={value}" for key, value in info.items())
                print(f"{module.VENDOR_SLUG} preflight: {safe_info}")
                available = info.get("available_credits")
                if (
                    module.VENDOR_SLUG == "fiber"
                    and isinstance(available, (int, float))
                    and available < len(pending[module.VENDOR_SLUG])
                ):
                    stopped[module.VENDOR_SLUG] = (
                        f"insufficient minimum credits: available={available}, "
                        f"minimum_needed={len(pending[module.VENDOR_SLUG])}"
                    )
                    print(f"{module.VENDOR_SLUG} {stopped[module.VENDOR_SLUG]}")
                    continue
            except Exception as exc:  # noqa: BLE001
                stopped[module.VENDOR_SLUG] = f"preflight failed: {type(exc).__name__}: {exc}"
                print(f"{module.VENDOR_SLUG} {stopped[module.VENDOR_SLUG]}")
                continue
        active_modules.append(module)

    if not active_modules:
        print("no provider has runnable missing cells")
    else:
        positions = {module.VENDOR_SLUG: 0 for module in active_modules}
        completed_now = {module.VENDOR_SLUG: 0 for module in active_modules}
        consecutive_transient = {module.VENDOR_SLUG: 0 for module in active_modules}
        last_planned_start = {module.VENDOR_SLUG: 0.0 for module in active_modules}
        writes_since_checkpoint = 0

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(active_modules)) as executor:
            futures: dict[concurrent.futures.Future[ProviderResult], tuple[Any, CompanyCase]] = {}

            def submit_next(module: Any) -> None:
                slug = module.VENDOR_SLUG
                if slug in stopped or positions[slug] >= len(pending[slug]):
                    return
                case = pending[slug][positions[slug]]
                positions[slug] += 1
                now = time.monotonic()
                interval = MIN_START_INTERVAL_SECONDS.get(slug, 0.0)
                planned_start = max(now, last_planned_start[slug] + interval)
                delay = max(0.0, planned_start - now)
                last_planned_start[slug] = planned_start
                futures[executor.submit(_call_provider_after_delay, module, case, delay)] = (module, case)

            for module in active_modules:
                submit_next(module)

            while futures:
                done, _ = concurrent.futures.wait(
                    futures, return_when=concurrent.futures.FIRST_COMPLETED
                )
                for future in done:
                    module, case = futures.pop(future)
                    result = future.result()
                    result.metrics = benchmark_metrics(case, result)
                    payload = result.to_dict()
                    _write_cell(payload)
                    _append_attempt(payload)
                    _merge_run(snapshot["runs"], indexes, payload)
                    completed_now[module.VENDOR_SLUG] += 1
                    writes_since_checkpoint += 1

                    usage = (
                        f", usage={result.cost_units:g} {result.cost_unit}"
                        if result.cost_units is not None and result.cost_unit
                        else ""
                    )
                    print(
                        f"[{module.VENDOR_SLUG} {completed_now[module.VENDOR_SLUG]}/"
                        f"{len(pending[module.VENDOR_SLUG])}] {case.input_domain}: "
                        f"{result.status}, {result.latency_ms}ms{usage}",
                        flush=True,
                    )

                    audit = result.audit or {}
                    transient = bool(audit.get("retriable"))
                    if result.status == "error" and transient:
                        consecutive_transient[module.VENDOR_SLUG] += 1
                    else:
                        consecutive_transient[module.VENDOR_SLUG] = 0

                    if result.status == "error" and audit.get("stop_provider"):
                        stopped[module.VENDOR_SLUG] = (
                            f"circuit open after HTTP {audit.get('http_status')} "
                            f"({audit.get('error_kind')})"
                        )
                    elif consecutive_transient[module.VENDOR_SLUG] >= 3:
                        stopped[module.VENDOR_SLUG] = "circuit open after 3 consecutive transient errors"

                    if module.VENDOR_SLUG in stopped:
                        print(f"{module.VENDOR_SLUG}: {stopped[module.VENDOR_SLUG]}", flush=True)
                    else:
                        submit_next(module)

                    if writes_since_checkpoint >= args.checkpoint_every:
                        _checkpoint(snapshot)
                        writes_since_checkpoint = 0

    selected_target_keys = {
        (case.case_slug, module.VENDOR_SLUG)
        for case in cases
        for module in modules
    }
    target_keys = {
        (case.case_slug, provider_slug)
        for case in cases
        for provider_slug in REGISTRY
    }
    current_rows = {
        (row["case_slug"], row["provider_slug"]): row
        for row in snapshot.get("runs") or []
    }
    missing_keys = target_keys - set(current_rows)
    selected_rows = [current_rows[key] for key in target_keys if key in current_rows]
    status_counts: dict[str, int] = {}
    for row in selected_rows:
        status = row.get("status") or "unknown"
        status_counts[status] = status_counts.get(status, 0) + 1
    retryable_errors = sum(
        1
        for row in selected_rows
        if row.get("status") == "error" and (row.get("audit") or {}).get("retriable")
    )
    nonretryable_errors = status_counts.get("error", 0) - retryable_errors
    pending_count = status_counts.get("pending", 0)
    if missing_keys or retryable_errors or pending_count:
        snapshot["status"] = "needs_retry"
    elif nonretryable_errors:
        snapshot["status"] = "complete_with_errors"
    else:
        snapshot["status"] = "complete"
    snapshot["execution_summary"] = {
        "selected_provider_count": len(modules),
        "selected_target_cells": len(selected_target_keys),
        "target_cells": len(target_keys),
        "recorded_cells": len(selected_rows),
        "missing_cells": len(missing_keys),
        "status_counts": status_counts,
        "retryable_errors": retryable_errors,
        "nonretryable_errors": nonretryable_errors,
        "stopped_providers": stopped,
        "automatic_retries": 0,
        "attempt_log": str(ATTEMPT_LOG.relative_to(Path(__file__).resolve().parents[1])),
    }
    _checkpoint(snapshot)

    print(f"final status: {snapshot['status']}; statuses={status_counts}; missing={len(missing_keys)}")
    if stopped:
        print("stopped providers: " + "; ".join(f"{slug}={reason}" for slug, reason in stopped.items()))
    print(f"snapshot: {SNAPSHOT_PATH}")
    print(f"attempt log: {ATTEMPT_LOG}")
    return 0 if snapshot["status"] in {"complete", "complete_with_errors"} else 4


if __name__ == "__main__":
    raise SystemExit(main())
