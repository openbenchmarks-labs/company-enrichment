"""Shared contract, cohort loading, reference scoring, and snapshot I/O.

The active benchmark is the frozen 300-company cohort whose website-to-LinkedIn
identities were manually verified. Only available LinkedIn fields enter scoring.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from statistics import median
from typing import Any
from urllib.parse import urlparse

import pycountry


ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_PATH = ROOT / "data" / "latest-firmographic.json"
RUNS_DIR = ROOT / "data" / "firmographic-runs"
GROUND_TRUTH_PATH = ROOT / "data" / "firmographic" / "company-ground-truth-v1.json"

DATASET_SLUG = "company-firmographic-linkedin-gt-2026-q3-v1"
DATASET_NAME = "Company Firmographic Enrichment — 300-Company Cohort"
LINKEDIN_REFERENCE_STATUS = "human_verified_linkedin_ground_truth_v1"

SCORED_ATTRIBUTES = (
    "legal_name",
    "primary_domain",
    "hq_location",
    "founded_year",
    "industry",
    "linkedin_url",
    "headcount_band",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _year(value: Any) -> int | None:
    if value in (None, ""):
        return None
    match = re.search(r"(?:17|18|19|20)\d{2}", str(value))
    return int(match.group(0)) if match else None


def normalize_domain(value: Any) -> str | None:
    if not value:
        return None
    raw = str(value).strip().lower()
    if "://" not in raw:
        raw = "https://" + raw
    host = (urlparse(raw).hostname or "").lower().rstrip(".")
    return host.removeprefix("www.") or None


def normalize_linkedin(value: Any) -> str | None:
    if not value:
        return None
    raw = str(value).strip()
    if "://" not in raw:
        raw = "https://" + raw.lstrip("/")
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if "linkedin.com" not in host:
        return raw.rstrip("/")
    path = re.sub(r"/+", "/", parsed.path).rstrip("/").lower()
    return f"https://www.linkedin.com{path}" if path else None


def normalize_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip()
    return re.sub(r"\s+", " ", text) or None


def normalize_country(value: Any) -> str | None:
    """Return an ISO-backed comparison value for names and alpha-2/3 codes."""
    if value in (None, ""):
        return None
    raw = str(value).strip()
    aliases = {
        "turkey": "Türkiye",
        "uk": "United Kingdom",
        "u.k.": "United Kingdom",
        "usa": "United States",
        "u.s.": "United States",
        "united states of america": "United States",
    }
    candidate = aliases.get(raw.lower(), raw)
    try:
        return pycountry.countries.lookup(candidate).alpha_2.lower()
    except LookupError:
        return normalize_text(candidate)


def normalize_funding_stage(value: Any) -> str | None:
    text = normalize_text(value)
    if not text:
        return None
    aliases = {
        "no funding yet": "unfunded",
        "no funding": "unfunded",
        "not funded": "unfunded",
        "pre seed": "pre_seed",
        "series unknown": "venture_unknown",
        "venture unknown": "venture_unknown",
        "private equity": "private_equity",
    }
    return aliases.get(text, text.replace(" ", "_"))


def _clean_int(value: Any) -> int | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return None


HEADCOUNT_BANDS: tuple[tuple[int, int | None], ...] = (
    (1, 1),
    (2, 10),
    (11, 50),
    (51, 200),
    (201, 500),
    (501, 1_000),
    (1_001, 5_000),
    (5_001, 10_000),
    (10_001, None),
)


def _canonical_headcount_band(
    minimum: int | None, maximum: int | None
) -> tuple[int, int | None] | None:
    if minimum is None and maximum is None:
        return None
    candidate = (minimum, maximum)
    if candidate in HEADCOUNT_BANDS:
        return candidate
    if minimum is not None and maximum == minimum:
        for lower, upper in HEADCOUNT_BANDS:
            if minimum >= lower and (upper is None or minimum <= upper):
                return lower, upper
    return None


@dataclasses.dataclass(frozen=True)
class CompanyCase:
    case_slug: str
    company_id: str
    slice: str
    input_name: str | None
    input_domain: str
    reference: dict[str, Any]
    source_metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class NormalizedCompany:
    legal_name: str | None = None
    primary_domain: str | None = None
    aliases: list[str] = dataclasses.field(default_factory=list)
    domains: list[str] = dataclasses.field(default_factory=list)
    hq_country: str | None = None
    hq_city: str | None = None
    founded_year: int | None = None
    industry: str | None = None
    industries: list[str] = dataclasses.field(default_factory=list)
    linkedin_url: str | None = None
    headcount_min: int | None = None
    headcount_max: int | None = None
    funding_stage: str | None = None

    def __post_init__(self) -> None:
        self.primary_domain = normalize_domain(self.primary_domain)
        self.domains = _unique(
            [d for value in [self.primary_domain, *self.domains] if (d := normalize_domain(value))]
        )
        self.aliases = _unique([str(v).strip() for v in self.aliases if str(v).strip()])
        self.linkedin_url = normalize_linkedin(self.linkedin_url)
        self.founded_year = _year(self.founded_year)
        self.headcount_min = _clean_int(self.headcount_min)
        self.headcount_max = _clean_int(self.headcount_max)
        if self.headcount_min is not None and self.headcount_min <= 0:
            self.headcount_min = None
        if self.headcount_max is not None and self.headcount_max <= 0:
            self.headcount_max = None
        self.funding_stage = normalize_funding_stage(self.funding_stage)
        self.industries = _unique(
            [str(v).strip() for v in [self.industry, *self.industries] if v and str(v).strip()]
        )
        if not self.industry and self.industries:
            self.industry = self.industries[0]

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class ProviderResult:
    provider_slug: str
    provider_name: str
    case_slug: str
    status: str
    latency_ms: int
    normalized: NormalizedCompany | None = None
    cost_units: float | None = None
    cost_unit: str | None = None
    ambiguity_count: int = 0
    audit: dict[str, Any] = dataclasses.field(default_factory=dict)
    error: str | None = None
    queried_at: str = dataclasses.field(default_factory=now_iso)
    metrics: list[dict[str, Any]] = dataclasses.field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        value = dataclasses.asdict(self)
        value["normalized"] = self.normalized.to_dict() if self.normalized else None
        return value


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def load_all_cases(
    *,
    ground_truth_path: Path = GROUND_TRUTH_PATH,
) -> tuple[list[CompanyCase], dict[str, Any]]:
    """Load the frozen 300-company cohort directly from the public ground truth."""
    manifest = json.loads(ground_truth_path.read_text(encoding="utf-8"))
    records = manifest.get("companies") or []
    if (
        manifest.get("status") != "frozen"
        or manifest.get("company_count") != 300
        or len(records) != 300
    ):
        raise ValueError("ground truth is not the frozen 300-company cohort")

    digest = hashlib.sha256(ground_truth_path.read_bytes()).hexdigest()
    reference_file = str(ground_truth_path.relative_to(ROOT))
    cases: list[CompanyCase] = []
    for record in records:
        domain = normalize_domain(record.get("input_domain"))
        reference = record.get("reference") or {}
        if not domain:
            raise ValueError(f"case {record.get('case_slug')} has no valid input domain")
        cases.append(
            CompanyCase(
                case_slug=record["case_slug"],
                company_id=record["case_slug"],
                slice=record["slice"],
                input_name=record.get("input_name") or reference.get("legal_name"),
                input_domain=domain,
                reference=reference,
                source_metadata={
                    "reference_status": LINKEDIN_REFERENCE_STATUS,
                    "reference_source": "manually reviewed company websites and LinkedIn pages",
                    "reference_file": reference_file,
                    "reference_sha256": digest,
                    "identity_status": "human_verified",
                },
            )
        )

    case_slugs = [case.case_slug for case in cases]
    domains = [case.input_domain for case in cases]
    if len(case_slugs) != len(set(case_slugs)):
        raise ValueError("frozen cohort contains duplicate case slugs")
    if len(domains) != len(set(domains)):
        raise ValueError("frozen cohort contains duplicate input domains")

    slice_counts: dict[str, int] = {}
    for case in cases:
        slice_counts[case.slice] = slice_counts.get(case.slice, 0) + 1
    if slice_counts != manifest.get("slice_counts"):
        raise ValueError("ground-truth slice counts do not match the frozen manifest")

    return cases, {
        "reference_status": LINKEDIN_REFERENCE_STATUS,
        "reference_source": "manually reviewed company websites and LinkedIn pages",
        "reference_file": reference_file,
        "reference_sha256": digest,
        "reference_rows": len(cases),
        "selected_total": len(cases),
        "slice_counts": slice_counts,
    }


def _present(company: NormalizedCompany, attribute: str) -> bool:
    if attribute == "hq_location":
        return bool(company.hq_country or company.hq_city)
    if attribute == "headcount_band":
        return company.headcount_min is not None or company.headcount_max is not None
    if attribute == "industry":
        return bool(company.industry or company.industries)
    return bool(getattr(company, attribute))


def _industry_agrees(reference: list[str], actual: list[str]) -> bool | None:
    ref = [normalize_text(v) for v in reference if normalize_text(v)]
    got = [normalize_text(v) for v in actual if normalize_text(v)]
    if not ref or not got:
        return None
    return any(r == g or (len(r) >= 5 and (r in g or g in r)) for r in ref for g in got)


def _reference_available(reference: dict[str, Any], attribute: str) -> bool:
    if attribute == "hq_location":
        return bool(reference.get("hq_country") or reference.get("hq_city"))
    if attribute == "headcount_band":
        return reference.get("headcount_min") is not None or reference.get("headcount_max") is not None
    if attribute == "industry":
        return bool(reference.get("industry") or reference.get("industries"))
    return reference.get(attribute) not in (None, "")


def reference_metrics(case: CompanyCase, result: ProviderResult) -> list[dict[str, Any]]:
    """Score a provider result only against reference fields available per case."""
    company = result.normalized
    status_detail = {
        "reference_status": case.source_metadata.get("reference_status")
        or "unknown_reference"
    }
    metrics: list[dict[str, Any]] = [
        {
            "metric_name": "resolved",
            "metric_value": 1 if company else 0,
            "detail": status_detail,
        }
    ]
    present = {
        attribute: bool(company and _present(company, attribute))
        for attribute in SCORED_ATTRIBUTES
    }
    for attribute, value in present.items():
        metrics.append(
            {
                "metric_name": f"coverage_{attribute}",
                "metric_value": 1 if value else 0,
                "detail": status_detail,
            }
        )
    metrics.append(
        {
            "metric_name": "attribute_coverage_pct",
            "metric_value": round(100 * sum(present.values()) / len(SCORED_ATTRIBUTES), 2),
            "detail": {**status_detail, "attribute_count": len(SCORED_ATTRIBUTES)},
        }
    )

    reference = case.reference
    evaluable = [
        attribute for attribute in SCORED_ATTRIBUTES if _reference_available(reference, attribute)
    ]
    if not evaluable:
        return metrics

    comparisons: dict[str, bool] = {}
    if company:
        if "legal_name" in evaluable:
            comparisons["legal_name"] = bool(
                company.legal_name
                and normalize_text(reference.get("legal_name")) == normalize_text(company.legal_name)
            )
        if "primary_domain" in evaluable:
            comparisons["primary_domain"] = bool(
                company.domains and normalize_domain(reference.get("primary_domain")) in company.domains
            )
        if "hq_location" in evaluable:
            country_matches = (
                normalize_country(reference.get("hq_country")) == normalize_country(company.hq_country)
                if reference.get("hq_country")
                else True
            )
            city_matches = (
                normalize_text(reference.get("hq_city")) == normalize_text(company.hq_city)
                if reference.get("hq_city")
                else True
            )
            comparisons["hq_location"] = bool(
                present["hq_location"] and country_matches and city_matches
            )
        if "founded_year" in evaluable:
            comparisons["founded_year"] = bool(
                company.founded_year is not None
                and reference.get("founded_year") == company.founded_year
            )
        if "industry" in evaluable:
            comparisons["industry"] = bool(
                _industry_agrees(
                    reference.get("industries")
                    or ([reference["industry"]] if reference.get("industry") else []),
                    company.industries or ([company.industry] if company.industry else []),
                )
            )
        if "linkedin_url" in evaluable:
            comparisons["linkedin_url"] = bool(
                company.linkedin_url
                and normalize_linkedin(reference.get("linkedin_url")) == company.linkedin_url
            )
        if "headcount_band" in evaluable:
            comparisons["headcount_band"] = (
                _canonical_headcount_band(
                    reference.get("headcount_min"), reference.get("headcount_max")
                )
                == _canonical_headcount_band(company.headcount_min, company.headcount_max)
            )
    for attribute in evaluable:
        correct = comparisons.get(attribute, False)
        metrics.append(
            {
                "metric_name": f"reference_correct_{attribute}",
                "metric_value": 1 if correct else 0,
                "detail": status_detail,
            }
        )

    returned = [attribute for attribute in evaluable if present[attribute]]
    correct_count = sum(1 for attribute in evaluable if comparisons.get(attribute, False))
    if returned:
        metrics.append(
            {
                "metric_name": "reference_accuracy_when_present_pct",
                "metric_value": round(100 * correct_count / len(returned), 2),
                "detail": {
                    **status_detail,
                    "truth_fields_evaluable": len(evaluable),
                    "truth_fields_returned": len(returned),
                },
            }
        )
    metrics.append(
        {
            "metric_name": "correct_field_yield_pct",
            "metric_value": round(100 * correct_count / len(evaluable), 2),
            "detail": {
                **status_detail,
                "truth_fields_evaluable": len(evaluable),
                "truth_fields_returned": len(returned),
            },
        }
    )
    return metrics


def benchmark_metrics(case: CompanyCase, result: ProviderResult) -> list[dict[str, Any]]:
    if case.source_metadata.get("reference_status") != LINKEDIN_REFERENCE_STATUS:
        raise ValueError(f"case {case.case_slug} does not have the frozen LinkedIn reference")
    return reference_metrics(case, result)


def metric_value(result: dict[str, Any], name: str) -> float | None:
    for metric in result.get("metrics") or []:
        if metric.get("metric_name") == name:
            value = metric.get("metric_value")
            return float(value) if value is not None else None
    return None


def _sql_round(value: float, places: int) -> float:
    """Match PostgreSQL numeric round (half away from zero)."""
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def recompute_leaderboard(runs: list[dict[str, Any]], case_count: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for provider_slug in sorted({run["provider_slug"] for run in runs}):
        cells = [run for run in runs if run["provider_slug"] == provider_slug]
        coverage = [v for run in cells if (v := metric_value(run, "attribute_coverage_pct")) is not None]
        reference_accuracy = [
            v
            for run in cells
            if (v := metric_value(run, "reference_accuracy_when_present_pct")) is not None
        ]
        correct_yield = [
            v for run in cells if (v := metric_value(run, "correct_field_yield_pct")) is not None
        ]
        latencies = [int(run["latency_ms"]) for run in cells if run.get("latency_ms") is not None]
        costs: dict[str, float] = {}
        for run in cells:
            if run.get("cost_unit") and run.get("cost_units") is not None:
                unit = run["cost_unit"]
                costs[unit] = round(costs.get(unit, 0) + float(run["cost_units"]), 4)
        rows.append(
            {
                "provider_slug": provider_slug,
                "provider_name": cells[0]["provider_name"],
                "cases_attempted": len(cells),
                "total_cases": case_count,
                "cases_resolved": sum(1 for run in cells if metric_value(run, "resolved") == 1),
                "resolution_rate_pct": _sql_round(
                    100 * sum(1 for run in cells if metric_value(run, "resolved") == 1) / case_count, 2
                ) if case_count else None,
                "avg_attribute_coverage_pct": _sql_round(sum(coverage) / len(coverage), 2) if coverage else None,
                "avg_reference_accuracy_when_present_pct": (
                    _sql_round(sum(reference_accuracy) / len(reference_accuracy), 2)
                    if reference_accuracy
                    else None
                ),
                "avg_correct_field_yield_pct": (
                    _sql_round(sum(correct_yield) / len(correct_yield), 2) if correct_yield else None
                ),
                "median_latency_ms": _sql_round(median(latencies), 1) if latencies else None,
                "usage_by_unit": costs,
            }
        )
    rows.sort(
        key=lambda row: (
            -(
                row["avg_correct_field_yield_pct"]
                if row["avg_correct_field_yield_pct"] is not None
                else row["avg_attribute_coverage_pct"] or -1
            ),
            -(row["resolution_rate_pct"] or -1),
            -(row["avg_attribute_coverage_pct"] or -1),
            row["provider_slug"],
        )
    )
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
    return rows


def write_snapshot(snapshot: dict[str, Any]) -> None:
    temporary = SNAPSHOT_PATH.with_suffix(SNAPSHOT_PATH.suffix + ".tmp")
    temporary.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(SNAPSHOT_PATH)


def read_snapshot() -> dict[str, Any]:
    return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
