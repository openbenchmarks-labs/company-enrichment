#!/usr/bin/env python3
"""Run resumable full-cohort web-research enrichment locally.

Each provider/case response is checkpointed immediately under outputs/; reruns
skip saved successful results. This never writes the published snapshot or DB.
"""

from __future__ import annotations

import argparse
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from firmographic.common import CompanyCase
from firmographic.providers import exa_research, exa_research_v2, parallel_research


ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "data" / "latest-firmographic.json"
OUTPUT_ROOT = ROOT / "data" / "firmographic-runs" / "web-research-v2"
PROVIDERS = {
    parallel_research.VENDOR_SLUG: parallel_research,
    exa_research.VENDOR_SLUG: exa_research,
    exa_research_v2.VENDOR_SLUG: exa_research_v2,
}
CONCURRENCY = {parallel_research.VENDOR_SLUG: 8, exa_research.VENDOR_SLUG: 12, exa_research_v2.VENDOR_SLUG: 5}


def output_path(provider: str, case: CompanyCase) -> Path:
    return OUTPUT_ROOT / "raw" / provider / f"{case.case_slug}.json"


def atomic_write(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(row, indent=2) + "\n")
    temp.replace(path)


def load_cases(limit: int | None) -> list[CompanyCase]:
    snapshot = json.loads(SNAPSHOT.read_text())
    cases = [CompanyCase(**row) for row in snapshot["cases"]]
    if len(cases) != 282:
        raise RuntimeError(f"expected frozen 282-company cohort, found {len(cases)}")
    return cases[:limit] if limit else cases


def run_one(module: Any, case: CompanyCase) -> dict[str, Any]:
    try:
        row = module.run(case).to_dict()
    except Exception as exc:  # failures are saved and retried on the next --resume run
        row = {
            "provider_slug": module.VENDOR_SLUG,
            "provider_name": module.VENDOR_NAME,
            "case_slug": case.case_slug,
            "input_name": case.input_name,
            "input_domain": case.input_domain,
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
        }
    atomic_write(output_path(module.VENDOR_SLUG, case), row)
    return row


def successful(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        return json.loads(path.read_text()).get("status") in {"ok", "not_found"}
    except (OSError, json.JSONDecodeError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-paid", action="store_true", help="required before live provider calls")
    parser.add_argument("--only", default=",".join(PROVIDERS), help="comma-separated provider slugs")
    parser.add_argument("--limit", type=int, help="first N frozen cases; omit for all 282")
    parser.add_argument("--no-resume", action="store_true", help="rerun saved successful cases")
    args = parser.parse_args()
    if not args.confirm_paid:
        parser.error("live provider calls require --confirm-paid")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    requested = [slug.strip() for slug in args.only.split(",") if slug.strip()]
    unknown = sorted(set(requested) - set(PROVIDERS))
    if unknown:
        parser.error(f"unknown provider(s): {', '.join(unknown)}")

    load_dotenv(ROOT / ".env.local", override=False)
    load_dotenv(ROOT / ".env.exa.local", override=False)
    modules = [PROVIDERS[slug] for slug in requested]
    missing = [key for module in modules for key in module.REQUIRED_ENV if not os.environ.get(key)]
    if missing:
        raise RuntimeError(f"missing required environment variable(s): {', '.join(missing)}")
    cases = load_cases(args.limit)

    all_rows: list[dict[str, Any]] = []
    for module in modules:
        pending = [case for case in cases if args.no_resume or not successful(output_path(module.VENDOR_SLUG, case))]
        print(json.dumps({"provider": module.VENDOR_SLUG, "cases": len(cases), "saved_successes": len(cases) - len(pending), "to_run": len(pending), "concurrency": CONCURRENCY[module.VENDOR_SLUG]}), flush=True)
        with ThreadPoolExecutor(max_workers=CONCURRENCY[module.VENDOR_SLUG]) as pool:
            futures = {pool.submit(run_one, module, case): case for case in pending}
            for index, future in enumerate(as_completed(futures), start=1):
                row = future.result()
                all_rows.append(row)
                print(json.dumps({"provider": module.VENDOR_SLUG, "completed": index, "of": len(pending), "domain": row.get("input_domain"), "status": row["status"], "latency_ms": row.get("latency_ms")}), flush=True)

    summary = {
        "providers": requested,
        "cases": len(cases),
        "newly_completed": len(all_rows),
        "ok": sum(row["status"] == "ok" for row in all_rows),
        "not_found": sum(row["status"] == "not_found" for row in all_rows),
        "errors": sum(row["status"] == "error" for row in all_rows),
        "raw_root": str(OUTPUT_ROOT.relative_to(ROOT)),
    }
    atomic_write(OUTPUT_ROOT / "latest-run-summary.json", summary)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
