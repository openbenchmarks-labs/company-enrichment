from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from firmographic.common import CompanyCase
from firmographic.providers import seltz_companies


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


def _response(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "legal_name": "Example, Inc.",
        "primary_domain": "example.com",
        "hq_country": "United States",
        "hq_city": "Austin",
        "founded_year": 2014,
        "industry": "Software Development",
        "industries": ["Software Development"],
        "linkedin_url": "https://www.linkedin.com/company/example",
        "headcount_min": 11,
        "headcount_max": 50,
    }
    value.update(overrides)
    return {
        "answer": json.dumps(value),
        "citations": [
            {"url": "https://example.com/about", "content": "Company profile"},
            {"url": "https://example.com/about", "content": "duplicate"},
        ],
    }


class SeltzAnswerAdapterTests(unittest.TestCase):
    def test_uses_companies_scope_and_does_not_leak_reference_data(self) -> None:
        case = _case(reference={"legal_name": "GROUND TRUTH SECRET"})
        body = seltz_companies.payload(case)
        self.assertEqual("companies", body["scope"])
        self.assertEqual("json_schema", body["response_format"]["type"])
        self.assertTrue(body["response_format"]["json_schema"]["strict"])
        self.assertNotIn("GROUND TRUTH SECRET", json.dumps(body))

    @patch.dict("os.environ", {"SELTZ_API_KEY": "test-secret"})
    @patch("firmographic.providers.seltz_companies.request_json")
    def test_normalizes_structured_answer_and_citations(self, request_mock) -> None:
        request_mock.return_value = (_response(), 321)

        result = seltz_companies.run(_case())

        self.assertEqual("ok", result.status)
        self.assertEqual("Seltz", result.provider_name)
        self.assertEqual("example.com", result.normalized.primary_domain)
        self.assertEqual((11, 50), (result.normalized.headcount_min, result.normalized.headcount_max))
        self.assertEqual(["https://example.com/about"], result.audit["sources"])
        self.assertEqual("companies", result.audit["scope"])
        self.assertEqual("seltz_answer_request", result.cost_unit)
        args, kwargs = request_mock.call_args
        self.assertEqual(("POST", "https://api.seltz.ai/v1/answer"), args)
        self.assertEqual("test-secret", kwargs["headers"]["x-api-key"])
        self.assertEqual(180, kwargs["timeout"])

    def test_accepts_fenced_json(self) -> None:
        response = _response()
        parsed = seltz_companies.parse_answer(f"```json\n{response['answer']}\n```", [])
        self.assertEqual("example.com", parsed["primary_domain"])

    @patch.dict("os.environ", {"SELTZ_API_KEY": "test-secret"})
    @patch("firmographic.providers.seltz_companies.request_json")
    def test_empty_structured_record_is_not_found(self, request_mock) -> None:
        request_mock.return_value = (
            _response(
                legal_name=None,
                primary_domain=None,
                hq_country=None,
                hq_city=None,
                founded_year=None,
                industry=None,
                industries=[],
                linkedin_url=None,
                headcount_min=None,
                headcount_max=None,
            ),
            99,
        )

        result = seltz_companies.run(_case())

        self.assertEqual("not_found", result.status)
        self.assertIsNone(result.normalized)

    def test_invalid_answer_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "not valid structured JSON"):
            seltz_companies.parse_answer("Here is some prose", [])


if __name__ == "__main__":
    unittest.main()
