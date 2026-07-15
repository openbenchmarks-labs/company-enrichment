from __future__ import annotations

import unittest
from unittest.mock import patch

from firmographic.common import CompanyCase
from firmographic.providers import (
    apollo,
    company_enrich,
    explorium,
    fiber,
    ocean_enrichment,
    people_data_labs,
    predictleads,
)


def _case() -> CompanyCase:
    return CompanyCase(
        case_slug="example",
        company_id="example",
        slice="stable_large",
        input_name="Example",
        input_domain="example.com",
        reference={},
        source_metadata={},
    )


class PeopleDataLabsAdapterTests(unittest.TestCase):
    @patch.dict("os.environ", {"PEOPLE_DATA_LABS_API_KEY": "test"})
    @patch("firmographic.providers.people_data_labs.request_json_with_headers")
    def test_normalizes_company_and_credit_header(self, request_mock) -> None:
        request_mock.return_value = (
            {
                "id": "company-1",
                "legal_name": "Example, Inc.",
                "website": "https://www.example.com/",
                "location": {"country": "United States", "locality": "Boston"},
                "founded": 2012,
                "industry": "Software",
                "linkedin_url": "linkedin.com/company/example",
                "size": "51-200",
                "latest_funding_stage": "Series A",
            },
            125,
            {"X-Call-Credits-Spent": "1"},
        )

        result = people_data_labs.run(_case())

        self.assertEqual("ok", result.status)
        self.assertEqual(1.0, result.cost_units)
        self.assertEqual("pdl_company_enrichment_credit", result.cost_unit)
        self.assertEqual("example.com", result.normalized.primary_domain)
        self.assertEqual((51, 200), (result.normalized.headcount_min, result.normalized.headcount_max))
        self.assertEqual("series_a", result.normalized.funding_stage)


class OceanAdapterTests(unittest.TestCase):
    @patch.dict("os.environ", {"OCEAN_API_KEY": "test"})
    @patch("firmographic.providers.ocean_enrichment.request_json")
    def test_normalizes_direct_response_and_body_credits(self, request_mock) -> None:
        request_mock.return_value = (
            {
                "domain": "example.com",
                "legalName": "Example Ltd.",
                "primaryCountry": "US",
                "locations": [{"primary": True, "city": "Boston", "country": "US"}],
                "yearFounded": 2012,
                "industries": ["Software Development"],
                "medias": {"linkedin": "example"},
                "companySize": "51-200",
                "fundingRound": {"type": "Seed"},
                "creditsUsed": 0.1,
            },
            140,
        )

        result = ocean_enrichment.run(_case())

        self.assertEqual("ok", result.status)
        self.assertEqual(0.1, result.cost_units)
        self.assertEqual("ocean_credit", result.cost_unit)
        self.assertEqual("https://www.linkedin.com/company/example", result.normalized.linkedin_url)
        self.assertEqual((51, 200), (result.normalized.headcount_min, result.normalized.headcount_max))
        self.assertEqual("seed", result.normalized.funding_stage)


class CompanyEnrichAdapterTests(unittest.TestCase):
    @patch.dict("os.environ", {"COMPANY_ENRICH_API_KEY": "test"})
    @patch("firmographic.providers.company_enrich.request_json")
    def test_normalizes_standard_enrichment_without_workforce_expansion(self, request_mock) -> None:
        request_mock.return_value = (
            {
                "id": "company-1",
                "name": "Example, Inc.",
                "domain": "example.com",
                "industry": "Software",
                "industries": ["Software", "Technology"],
                "employees": "501-1K",
                "founded_year": 2012,
                "location": {
                    "country": {"code": "US", "name": "United States"},
                    "city": {"name": "Boston"},
                },
                "socials": {"linkedin_url": "https://linkedin.com/company/example"},
                "financial": {"funding_stage": "Series A"},
                "updated_at": "2026-07-01T00:00:00Z",
            },
            160,
        )

        result = company_enrich.run(_case())

        self.assertEqual("ok", result.status)
        self.assertEqual(1.0, result.cost_units)
        self.assertEqual("company_enrich_credit", result.cost_unit)
        self.assertEqual("United States", result.normalized.hq_country)
        self.assertEqual((501, 1_000), (result.normalized.headcount_min, result.normalized.headcount_max))
        self.assertEqual("series_a", result.normalized.funding_stage)
        request_mock.assert_called_once()
        self.assertNotIn("expand", request_mock.call_args.kwargs["params"])


class ApolloAdapterTests(unittest.TestCase):
    @patch.dict("os.environ", {"APOLLO_API_KEY": "test"})
    @patch("firmographic.providers.apollo.request_json")
    def test_searches_then_fetches_the_matched_organization(self, request_mock) -> None:
        request_mock.side_effect = [
            ({"organizations": [{"id": "org-1"}]}, 100),
            (
                {
                    "organization": {
                        "id": "org-1",
                        "name": "Example, Inc.",
                        "primary_domain": "example.com",
                        "city": "Boston",
                        "country": "United States",
                        "founded_year": 2012,
                        "industries": ["Software"],
                        "linkedin_url": "https://linkedin.com/company/example",
                        "estimated_num_employees": 100,
                    }
                },
                125,
            ),
        ]

        result = apollo.run(_case())

        self.assertEqual("ok", result.status)
        self.assertEqual(225, result.latency_ms)
        self.assertEqual(2, result.cost_units)
        self.assertEqual((100, 100), (result.normalized.headcount_min, result.normalized.headcount_max))
        self.assertEqual(2, request_mock.call_count)


class ExploriumAdapterTests(unittest.TestCase):
    @patch.dict("os.environ", {"EXPLORIUM_API_KEY": "test"})
    @patch("firmographic.providers.explorium.request_json")
    def test_matches_then_fetches_the_selected_business(self, request_mock) -> None:
        request_mock.side_effect = [
            ({"matched_businesses": [{"business_id": "business-1"}]}, 90),
            (
                {
                    "data": [
                        {
                            "business_id": "business-1",
                            "company_name": "Example, Inc.",
                            "domain": "example.com",
                            "city": "Boston",
                            "country": "US",
                            "year_founded": 2012,
                            "industry": "Software",
                            "linkedin_url": "linkedin.com/company/example",
                            "number_of_employees_range": "51-200",
                        }
                    ]
                },
                110,
            ),
        ]

        result = explorium.run(_case())

        self.assertEqual("ok", result.status)
        self.assertEqual(200, result.latency_ms)
        self.assertEqual(2, result.cost_units)
        self.assertEqual("example.com", result.normalized.primary_domain)


class FiberAdapterTests(unittest.TestCase):
    @patch.dict("os.environ", {"FIBER_API_KEY": "test"})
    @patch("firmographic.providers.fiber.request_json")
    def test_normalizes_consensus_fields_and_reported_charge(self, request_mock) -> None:
        request_mock.return_value = (
            {
                "output": {
                    "data": [
                        {
                            "name_consensus": {"value": "Example, Inc."},
                            "domain_consensus": {"value": "example.com"},
                            "location_consensus": {
                                "value": {"city": "Boston", "country": "US"}
                            },
                            "employee_range_consensus": {"value": "51-200"},
                            "founded_year": 2012,
                            "industries": ["Software"],
                            "linkedin_primary_slug": "example",
                        }
                    ]
                },
                "chargeInfo": {"creditsCharged": 2},
            },
            130,
        )

        result = fiber.run(_case())

        self.assertEqual("ok", result.status)
        self.assertEqual(2, result.cost_units)
        self.assertEqual("fiber_credit", result.cost_unit)
        self.assertEqual("https://www.linkedin.com/company/example", result.normalized.linkedin_url)


class PredictLeadsAdapterTests(unittest.TestCase):
    @patch.dict(
        "os.environ",
        {"PREDICT_LEADS_API_KEY": "test", "PREDICT_LEADS_API_TOKEN": "test"},
    )
    @patch("firmographic.providers.predictleads.request_json")
    def test_normalizes_extended_company_response(self, request_mock) -> None:
        request_mock.return_value = (
            {
                "data": [
                    {
                        "id": "company-1",
                        "attributes": {
                            "domain": "example.com",
                            "company_name": "Example, Inc.",
                            "location_data": [
                                {"category": "headquarters", "country": "US", "city": "Boston"}
                            ],
                            "linkedin_founded": 2012,
                            "linkedin_industry": "Software",
                            "linkedin_url": "https://linkedin.com/company/example",
                            "employees_min": 51,
                            "employees_max": 200,
                        },
                        "relationships": {"redirects_to": {"data": None, "meta": {}}},
                    }
                ],
                "included": [],
                "meta": {"schema_version": "3.7"},
            },
            150,
        )

        result = predictleads.run(_case())

        self.assertEqual("ok", result.status)
        self.assertEqual(1, result.cost_units)
        self.assertEqual("extended_company_request", result.cost_unit)
        self.assertEqual((51, 200), (result.normalized.headcount_min, result.normalized.headcount_max))


if __name__ == "__main__":
    unittest.main()
