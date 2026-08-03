from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import run_firmographic_full_benchmark as runner
from firmographic.common import load_all_cases
from firmographic.providers.base import ProviderHTTPError


class FullRunnerTests(unittest.TestCase):
    def test_resume_preserves_all_existing_cells(self) -> None:
        cases, manifest = load_all_cases()
        old_cases = [case.to_dict() for case in cases[:10]]
        old_runs = [
            {
                "provider_slug": provider,
                "provider_name": provider,
                "case_slug": case["case_slug"],
                "status": "ok",
                "latency_ms": 100,
                "normalized": {},
                "cost_units": 1,
                "cost_unit": "credit",
                "ambiguity_count": 0,
                "audit": {},
                "error": None,
                "queried_at": "2026-07-14T00:00:00Z",
                "metrics": [],
            }
            for case in old_cases
            for provider in runner.REGISTRY
        ]
        existing = {
            "dataset_slug": runner.DATASET_SLUG,
            "created_at": "2026-07-14T00:00:00Z",
            "cases": old_cases,
            "runs": old_runs,
        }
        with tempfile.TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "snapshot.json"
            snapshot_path.write_text("{}", encoding="utf-8")
            with (
                patch.object(runner, "SNAPSHOT_PATH", snapshot_path),
                patch.object(runner, "read_snapshot", return_value=existing),
                patch.object(runner, "_load_ledger", return_value=[]),
            ):
                snapshot, indexes = runner._prepare_snapshot(cases, manifest, mutate=False)
        self.assertEqual(282, len(snapshot["cases"]))
        self.assertEqual(70, len(snapshot["runs"]))
        self.assertEqual(70, len(indexes))

    def test_rate_limit_error_is_timed_and_stops_only_that_provider(self) -> None:
        cases, _ = load_all_cases()
        module = runner.REGISTRY["apollo"]
        error = ProviderHTTPError(
            429,
            "slow down",
            elapsed_ms=321,
            response_headers={"retry-after": "60"},
        )
        result = runner._error_result(cases[0], module, error, 400)
        self.assertEqual("error", result.status)
        self.assertEqual(321, result.latency_ms)
        self.assertEqual("rate_limited", result.audit["error_kind"])
        self.assertEqual("60", result.audit["retry_after"])
        self.assertTrue(result.audit["stop_provider"])


if __name__ == "__main__":
    unittest.main()
