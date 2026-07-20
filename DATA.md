# Data dictionary

## Frozen inputs and ground truth

`data/firmographic/company-inputs-v1.csv` contains the exact 300 domains sent to
every provider, their slices, stable case IDs, and the website-to-LinkedIn pairs
verified by the OpenBenchmarks team.

`data/firmographic/company-ground-truth-v1.json` is the independent scoring
reference. Each company has an identity status and the available values for:

| Benchmark field | Stored keys |
|---|---|
| Company name | `legal_name` |
| Primary domain | `primary_domain` |
| Headquarters | `hq_country`, `hq_city` |
| Founded year | `founded_year` |
| Industry | `industry`, `industries` |
| LinkedIn URL | `linkedin_url` |
| Headcount band | `headcount_min`, `headcount_max` |

Null reference values are excluded from that company's scoring denominator.

## Publication snapshot

`data/latest-firmographic.json` is the file behind the published benchmark. Its
main arrays are:

- `cases`: 300 frozen inputs and reference values
- `runs`: 2,100 normalized provider responses and field-level metrics
- `leaderboard`: seven provider aggregates derived from those cells

Each run includes the case/provider IDs, status, normalized response, latency,
usage unit, selected adapter audit metadata, error, query time, coverage, and
field-level judge decisions. `normalized` preserves every field produced by the
shared adapter contract, while only the seven documented fields are scored.

The provider runner did not retain the vendors' literal full HTTP response
bodies. The normalized output and all values used in scoring are present.

## Judge artifacts

`llm-judge-v3` contains the active GPT-5.6-terra (medium reasoning) structured-output pass: one
all-company call for Fiber and six 50-company chunks for each of the other six
providers (37 calls total), plus provider aggregates, manifests, token usage,
response IDs, field decisions, and rationales. The rubric was the same; the
batch shape changed after the first provider to make calls easier to resume.

`llm-industry-judge-v1` is retained as a historical dedicated industry pass. It applies the
same industry rubric across providers and records each final decision and
rationale. Six providers completed in one all-company call each; CompanyEnrich
completed in six recovery chunks. The provider aggregate files are the inputs
used by `--apply`, and the source-call checkpoints are retained for auditability.
The scripts contain the versioned prompts and response schemas.

## Cohort labels

Each input retains one of four sampling labels: `stable_large`, `long_tail`,
`subsidiary`, or `rebranded_or_domain_changed`. These labels explain the
coverage stress tested by the benchmark; they are not scored fields or expected
provider answers. The final selection logic is documented in the methodology.

## Cost and latency

Latency is stored per provider-company cell in milliseconds. Usage is stored in
the unit emitted or inferred by each adapter. `pricing-v1.json` contains the
dated public entry-tier conversions used for estimated USD cost.
