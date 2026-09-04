# Data dictionary

## Frozen inputs and Ground Truth

`data/firmographic/company-inputs-v2.csv` contains the exact 282 input domains
sent to every provider, stable case IDs, and cohort slices.

`data/firmographic/company-ground-truth-v2.json` is the compact independent
reference derived from the published snapshot. All fields were manually
refreshed and reviewed for this release across official company sources,
filings, registries, news, reputable reference sources, redirects, and
canonical or alternate LinkedIn URLs.

| Scored benchmark field | Stored keys |
|---|---|
| Headquarters location | `hq_country`, `hq_city` |
| Founded year | `founded_year` |
| Industry | `industry`, `industries` |
| LinkedIn URL | `linkedin_url`, `linkedin_url_alternates` where verified |
| Employee band / count | `headcount_min`, `headcount_max`, `headcount_exact` |

`legal_name`, `primary_domain`, and `alternate_primary_domains` are retained
for identity audit and judge context, but do not enter the metric denominator.
Blank Ground Truth is excluded from the respective field denominator.

## Publication snapshot

`data/latest-firmographic.json` is the published 282-company snapshot. Its
main arrays are:

- `cases`: frozen inputs and human-reviewed references
- `runs`: 2,538 normalized provider responses and field-level metrics
- `leaderboard`: nine provider aggregates calculated from those cells

Each run includes the provider/case IDs, status, normalized response, latency,
usage unit, selected request/response audit metadata, errors, coverage, and
final field-level judge decisions. The full snapshot does not contain secrets
or labeller review state.

## Judge artifacts and policy

The active rubric is the dedicated field judge in
`scripts/field_judge_prompts.py`. It uses `gpt-5.6-terra` at medium reasoning
and evaluates one field at a time for one provider, giving each decision a
short, value-specific rationale. The prompts encode the audited equivalences:

- ISO country names/codes, including England/UK/GB
- accepted city/metro aliases and specified localities
- LinkedIn alternate URLs, including matching `/company/` and `/school/` slugs
- semantic industry adjacency without treating materially different sectors as
  equivalent
- exact-count/range containment, ±5% exact-count tolerance, and narrow
  off-by-one employee ranges

## Cohort labels

Each input is labelled `stable_large`, `long_tail`, `subsidiary`, or
`rebranded_or_domain_changed`. They are coverage-stress slices, not scored
provider attributes.

## Historical files

The `*-v1` inputs, Ground Truth, pricing, and chunk-judge directories document
the prior 300-company release. They are retained for auditability and must not
be mixed with this v2 snapshot.
