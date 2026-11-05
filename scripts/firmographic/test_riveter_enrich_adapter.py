from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from firmographic.common import CompanyCase
from firmographic.providers import riveter_enrich
from firmographic.providers.base import ProviderHTTPError
from firmographic.web_research import SCORING_FIELDS


def _case(*, reference: dict | None = None) -> CompanyCase:
    return CompanyCase(
        case_slug="example",
        company_id="example",
        slice="long_tail",
        input_name="Example, Inc.",
        input_domain="example.com",
        reference=reference or {},
        source_metadata={},
    )


def _cell(value: object, *sources: str) -> dict[str, object]:
    if value is None:
        return {"value": None, "state": "not_found"}
    cell: dict[str, object] = {"value": value, "state": "ok"}
    if sources:
        cell["sources"] = list(sources)
    return cell


def _row(**overrides: dict[str, object]) -> dict[str, object]:
    cells: dict[str, object] = {
        "legal_name": _cell("Example, Inc.", "https://example.com/about"),
        "primary_domain": _cell("example.com"),
        "hq_country": _cell("United States", "https://example.com/about"),
        "hq_city": _cell("Austin", "https://www.linkedin.com/company/example"),
        "founded_year": _cell(2014),
        "industry": _cell("Software Development"),
        "industries": _cell(["Software Development", "SaaS"]),
        "linkedin_url": _cell("https://www.linkedin.com/company/example", "https://www.linkedin.com/company/example"),
        "headcount_min": _cell(11),
        "headcount_max": _cell(50),
    }
    cells.update(overrides)
    return {"id": "row_1", "position": 1, "input": {"Company Name": "Example, Inc.", "Company Domain": "example.com"}, "cells": cells}


class _FakeApi:
    """request_json stand-in keyed on (method, path); records every call."""

    def __init__(self, *, run_statuses: list[str], row: dict[str, object], kickoff_extra: dict | None = None) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.run_statuses = list(run_statuses)
        self.row = row
        self.kickoff_extra = kickoff_extra or {}

    def __call__(self, method: str, url: str, **kwargs: object) -> tuple[object, int]:
        path = url.removeprefix(riveter_enrich.API_BASE)
        self.calls.append((method, path, kwargs))
        if (method, path) == ("GET", "/tools"):
            return {"tools": [
                {"name": "linkedin_company_profile", "enabled": True},
                {"name": "linkedin_company_job_count", "enabled": False},
                {"name": "linkedin_company_posts", "enabled": True},
            ]}, 20
        if (method, path) == ("POST", "/enrichments"):
            return {"id": "enr_1", "name": "recipe", "tools": ["web_search", "scrape", "linkedin_company_data", "linkedin_company_profile"],
                    "budget_credits_per_row": 30, "budget_locked": True, "version": 1, "dataset_id": "ds_recipe"}, 30
        if (method, path) == ("POST", "/enrich"):
            return {"id": "run_1", "status": "enqueued", "dataset_id": "ds_1", **self.kickoff_extra}, 40
        if (method, path) == ("GET", "/runs/run_1"):
            status = self.run_statuses.pop(0) if self.run_statuses else "success"
            return {"id": "run_1", "status": status, "dataset_id": "ds_1", "credits_used": 3.2 if status == "success" else 0,
                    "stopped_reason": "credit_limit" if status == "stopped" else None, "error": None}, 50
        if (method, path) == ("GET", "/datasets/ds_1/rows"):
            return {"rows": [self.row], "next_cursor": None, "row_count": 1}, 25
        raise AssertionError(f"unexpected call {method} {path}")


class RiveterEnrichAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        riveter_enrich._SAVED_ENRICHMENT.clear()
        riveter_enrich._KICKOFF_TIMES.clear()
        patcher = patch("firmographic.providers.riveter_enrich.time.sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def test_kickoffs_are_paced_under_the_per_minute_admission_cap(self) -> None:
        clock = iter([0.0, 0.0, 1.0, 1.0, 2.0, 2.0, 61.0, 61.0])
        with patch.object(riveter_enrich, "KICKOFFS_PER_MINUTE", 2), patch("firmographic.providers.riveter_enrich.time.monotonic", side_effect=clock):
            riveter_enrich.wait_for_kickoff_slot()
            riveter_enrich.wait_for_kickoff_slot()
            self.sleep.assert_not_called()
            riveter_enrich.wait_for_kickoff_slot()
        self.sleep.assert_called_once()
        self.assertAlmostEqual(58.0, self.sleep.call_args.args[0])
        self.assertEqual([61.0], list(riveter_enrich._KICKOFF_TIMES))

    def test_recipe_is_the_shared_contract_as_typed_columns(self) -> None:
        body = riveter_enrich.recipe(["linkedin_company_profile"])
        self.assertEqual(list(SCORING_FIELDS), [column["name"] for column in body["output"]])
        self.assertEqual({"type": "integer"}, next(c for c in body["output"] if c["name"] == "founded_year")["output_schema"])
        self.assertEqual({"type": "array", "items": {"type": "string"}}, next(c for c in body["output"] if c["name"] == "industries")["output_schema"])
        self.assertTrue(all(column["prompt"] for column in body["output"]))
        self.assertEqual(["Company Name", "Company Domain"], body["input_columns"])
        self.assertEqual((30, True), (body["budget"], body["budget_locked"]))
        self.assertEqual(["linkedin_company_profile"], body["additional_tools"])
        self.assertNotIn("additional_tools", riveter_enrich.recipe([]))

    def test_kickoff_sends_only_the_identity_input(self) -> None:
        case = _case(reference={"legal_name": "GROUND TRUTH SECRET", "hq_city": "SECRET CITY"})
        body = riveter_enrich.kickoff_body(case, "enr_1")
        self.assertEqual({"enrichment_id": "enr_1", "input": {"Company Name": ["Example, Inc."], "Company Domain": ["example.com"]}}, body)
        self.assertNotIn("SECRET", json.dumps(body))

    @patch.dict("os.environ", {"RIVETER_API_KEY": "test-secret"})
    def test_saves_the_recipe_once_runs_the_row_and_reads_cells_and_sources(self) -> None:
        api = _FakeApi(run_statuses=["processing", "success"], row=_row())
        with patch("firmographic.providers.riveter_enrich.request_json", side_effect=api):
            result = riveter_enrich.run(_case())
            second = riveter_enrich.run(_case())

        self.assertEqual("ok", result.status)
        self.assertEqual("Riveter", result.provider_name)
        self.assertEqual("example.com", result.normalized.primary_domain)
        self.assertEqual(2014, result.normalized.founded_year)
        self.assertEqual((11, 50), (result.normalized.headcount_min, result.normalized.headcount_max))
        self.assertEqual("https://www.linkedin.com/company/example", result.normalized.linkedin_url)
        self.assertEqual(["Software Development", "SaaS"], result.normalized.industries)
        self.assertEqual(["https://example.com/about", "https://www.linkedin.com/company/example"], result.audit["sources"])
        self.assertEqual((3.2, "riveter_credit"), (result.cost_units, result.cost_unit))
        self.assertEqual("enr_1", result.audit["enrichment"]["id"])
        self.assertEqual("run_1", result.audit["run"]["id"])
        self.assertEqual("ok", second.status)

        paths = [(method, path) for method, path, _ in api.calls]
        self.assertEqual(1, paths.count(("GET", "/tools")))
        self.assertEqual(1, paths.count(("POST", "/enrichments")))
        self.assertEqual(2, paths.count(("POST", "/enrich")))
        create = next(kwargs for method, path, kwargs in api.calls if (method, path) == ("POST", "/enrichments"))
        self.assertEqual(["linkedin_company_profile", "linkedin_company_posts"], create["json_body"]["additional_tools"])
        kickoff = next(kwargs for method, path, kwargs in api.calls if (method, path) == ("POST", "/enrich"))
        self.assertEqual("enr_1", kickoff["json_body"]["enrichment_id"])
        self.assertEqual("Bearer test-secret", kickoff["headers"]["Authorization"])
        self.assertEqual(riveter_enrich.USER_AGENT, kickoff["headers"]["User-Agent"])
        self.assertEqual(90, kickoff["timeout"])
        poll = next(kwargs for method, path, kwargs in api.calls if (method, path) == ("GET", "/runs/run_1"))
        self.assertEqual({"wait": 50}, poll["params"])

    @patch.dict("os.environ", {"RIVETER_API_KEY": "test-secret"})
    def test_all_cells_not_found_is_not_found(self) -> None:
        empty = _row(**{key: _cell(None) for key in SCORING_FIELDS})
        api = _FakeApi(run_statuses=["success"], row=empty)
        with patch("firmographic.providers.riveter_enrich.request_json", side_effect=api):
            result = riveter_enrich.run(_case())
        self.assertEqual("not_found", result.status)
        self.assertIsNone(result.normalized)
        self.assertEqual(3.2, result.cost_units)

    @patch.dict("os.environ", {"RIVETER_API_KEY": "test-secret"})
    def test_stopped_run_is_an_error(self) -> None:
        api = _FakeApi(run_statuses=["stopped"], row=_row())
        with patch("firmographic.providers.riveter_enrich.request_json", side_effect=api):
            with self.assertRaisesRegex(ValueError, "ended stopped: credit_limit"):
                riveter_enrich.run(_case())

    @patch.dict("os.environ", {"RIVETER_API_KEY": "test-secret"})
    def test_legacy_pipeline_response_is_an_error(self) -> None:
        api = _FakeApi(run_statuses=["success"], row=_row(), kickoff_extra={"legacy": {"notice": "format is legacy"}})
        with patch("firmographic.providers.riveter_enrich.request_json", side_effect=api):
            with self.assertRaisesRegex(ValueError, "legacy pipeline"):
                riveter_enrich.run(_case())

    def test_failed_cell_is_an_error(self) -> None:
        row = _row(founded_year={"value": None, "state": "failed", "reason": "provider"})
        with self.assertRaisesRegex(ValueError, "failed: founded_year"):
            riveter_enrich.parse_row(row)

    @patch.dict("os.environ", {"RIVETER_API_KEY": "test-secret"})
    def test_poll_retries_server_errors_until_terminal(self) -> None:
        answers: list[object] = [ProviderHTTPError(502, "bad gateway"), {"id": "run_1", "status": "processing"}, {"id": "run_1", "status": "success"}]

        def fake(method: str, url: str, **kwargs: object) -> tuple[object, int]:
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer, 1

        with patch("firmographic.providers.riveter_enrich.request_json", side_effect=fake):
            self.assertEqual("success", riveter_enrich.poll_run("run_1")["status"])
        self.assertEqual([], answers)

    def test_poll_does_not_retry_client_errors(self) -> None:
        with patch("firmographic.providers.riveter_enrich.request_json", side_effect=ProviderHTTPError(404, "no run")):
            with patch.dict("os.environ", {"RIVETER_API_KEY": "test-secret"}):
                with self.assertRaises(ProviderHTTPError):
                    riveter_enrich.poll_run("run_missing")


if __name__ == "__main__":
    unittest.main()
