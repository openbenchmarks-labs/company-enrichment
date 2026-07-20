from __future__ import annotations

import unittest

from firmographic.common import (
    LINKEDIN_REFERENCE_STATUS,
    SCORED_ATTRIBUTES,
    CompanyCase,
    NormalizedCompany,
    ProviderResult,
    benchmark_metrics,
    load_all_cases,
    normalize_country,
    normalize_domain,
    normalize_linkedin,
)


def reference_case(reference: dict) -> CompanyCase:
    return CompanyCase(
        case_slug="case",
        company_id="case",
        slice="stable_large",
        input_name="Example",
        input_domain="example.com",
        reference=reference,
        source_metadata={"reference_status": LINKEDIN_REFERENCE_STATUS},
    )


class CohortTests(unittest.TestCase):
    def test_active_contract_has_exactly_seven_fields(self) -> None:
        self.assertEqual(7, len(SCORED_ATTRIBUTES))
        self.assertNotIn("funding_stage", SCORED_ATTRIBUTES)
        self.assertNotIn("operating_status", SCORED_ATTRIBUTES)

    def test_final_cohort_has_300_unique_domains_across_four_slices(self) -> None:
        cases, metadata = load_all_cases()
        self.assertEqual(300, len(cases))
        self.assertEqual(300, len({case.input_domain for case in cases}))
        self.assertEqual(
            {"stable_large", "long_tail", "subsidiary", "rebranded_or_domain_changed"},
            {case.slice for case in cases},
        )
        self.assertEqual(LINKEDIN_REFERENCE_STATUS, metadata["reference_status"])
        self.assertTrue(
            all(
                case.source_metadata["reference_status"] == LINKEDIN_REFERENCE_STATUS
                and case.source_metadata["identity_status"] == "human_verified"
                for case in cases
            )
        )

    def test_verified_group_cse_identity(self) -> None:
        cases, _ = load_all_cases()
        group_cse = next(case for case in cases if case.input_domain == "groupcse.com")
        self.assertEqual("groupcse.com", group_cse.reference["primary_domain"])
        self.assertEqual(
            "https://www.linkedin.com/company/group-cse",
            group_cse.reference["linkedin_url"],
        )

    def test_ground_truth_can_retain_deferred_reference_fields(self) -> None:
        cases, _ = load_all_cases()
        self.assertNotIn("funding_stage", SCORED_ATTRIBUTES)
        self.assertTrue(all("funding_stage" in case.reference for case in cases))


class NormalizationAndScoringTests(unittest.TestCase):
    def test_domain_and_linkedin_normalization(self) -> None:
        self.assertEqual("example.com", normalize_domain("https://www.Example.com/path"))
        self.assertEqual(
            "https://www.linkedin.com/company/example",
            normalize_linkedin("http://linkedin.com/company/Example/"),
        )

    def test_zero_headcount_is_missing(self) -> None:
        company = NormalizedCompany(headcount_min=0, headcount_max=0)
        self.assertIsNone(company.headcount_min)
        self.assertIsNone(company.headcount_max)

    def test_country_names_and_codes_compare_canonically(self) -> None:
        self.assertEqual(normalize_country("AU"), normalize_country("Australia"))
        self.assertEqual(normalize_country("USA"), normalize_country("United States"))
        self.assertEqual(normalize_country("Turkey"), normalize_country("Türkiye"))

    def test_missing_truth_is_excluded_and_missing_output_lowers_yield(self) -> None:
        case = reference_case(
            {
                "legal_name": "Example",
                "primary_domain": "example.com",
                "hq_country": "US",
                "hq_city": None,
                "founded_year": None,
                "industry": "Software",
                "industries": ["Software"],
                "linkedin_url": None,
                "headcount_min": 11,
                "headcount_max": 50,
            }
        )
        result = ProviderResult(
            "vendor",
            "Vendor",
            "case",
            "ok",
            100,
            normalized=NormalizedCompany(legal_name="Example", primary_domain="example.com"),
        )
        values = {
            metric["metric_name"]: metric["metric_value"]
            for metric in benchmark_metrics(case, result)
        }
        self.assertEqual(100, values["reference_accuracy_when_present_pct"])
        self.assertEqual(40, values["correct_field_yield_pct"])
        self.assertNotIn("reference_correct_founded_year", values)

    def test_unresolved_response_has_zero_coverage_and_yield(self) -> None:
        case = reference_case({"primary_domain": "example.com"})
        values = {
            metric["metric_name"]: metric["metric_value"]
            for metric in benchmark_metrics(
                case, ProviderResult("vendor", "Vendor", "case", "not_found", 123)
            )
        }
        self.assertEqual(0, values["attribute_coverage_pct"])
        self.assertEqual(0, values["correct_field_yield_pct"])

    def test_headcount_requires_the_same_standard_band(self) -> None:
        case = reference_case({"headcount_min": 51, "headcount_max": 200})
        exact = ProviderResult(
            "vendor", "Vendor", "case", "ok", 1,
            normalized=NormalizedCompany(headcount_min=100, headcount_max=100),
        )
        broad = ProviderResult(
            "vendor", "Vendor", "case", "ok", 1,
            normalized=NormalizedCompany(headcount_min=50, headcount_max=250),
        )
        exact_values = {
            metric["metric_name"]: metric["metric_value"]
            for metric in benchmark_metrics(case, exact)
        }
        broad_values = {
            metric["metric_name"]: metric["metric_value"]
            for metric in benchmark_metrics(case, broad)
        }
        self.assertEqual(100, exact_values["correct_field_yield_pct"])
        self.assertEqual(0, broad_values["correct_field_yield_pct"])


if __name__ == "__main__":
    unittest.main()
