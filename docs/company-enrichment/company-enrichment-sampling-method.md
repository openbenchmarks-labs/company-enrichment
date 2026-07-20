# Company Enrichment Sampling Method

- Status: 300-company final evaluation cohort frozen
- Cohort: `firmographic-2026-q3-v1`
- Method frozen through: 2026-07-14 (America/Los_Angeles)

## 1. Purpose

This document records the sampling and evaluation method used to construct the
company firmographic enrichment benchmark.
OpenSearch values and source-workbook notes are discovery hints, not benchmark
reference data.

The planned benchmark contains 500 unique companies across five slices:

| Slice | Target | Selected | Current source | Status |
|---|---:|---:|---|---|
| Stable large companies | 100 | 100 | Company OpenSearch index | Selected |
| Long-tail companies | 100 | 100 | Company OpenSearch index | Selected |
| Subsidiaries | 100 | 100 | Company OpenSearch index | Selected; source label still needs independent verification |
| Rebranded or domain-changed companies | 100 | 67 | Business Wire name-change feed and company/regulatory evidence | 67 verified; do not lower the bar to fill the remaining 33 |
| Acquired or shut-down companies | 100 | 0 | External event sources required | Deferred |
| **Total** | **500** | **367** |  | **133 still required** |

## 2. Evaluation scope

The benchmark will score these attributes:

- Company name
- Primary domain
- Headquarters country and city
- Founded year
- Industry
- LinkedIn URL
- Headcount band

The following are explicitly outside the current benchmark contract:

- Company operating status (provider coverage is too sparse)
- Funding stage (no reference values were returned for the final cohort)
- Revenue
- Growth signals
- Investors
- Department headcount
- Employee-growth rate
- Latest funding round
- Latest funding-round date

These exclusions prevent fast-changing or definition-sensitive fields from
weakening the first version of the evaluation contract.

### 2.1 Final evaluation cohort

The 367-company candidate frame remains the sampling audit trail. The final v1
evaluation set contains 300 companies:

1. The OpenBenchmarks team manually visited each candidate website and LinkedIn
   company page and confirmed that the pair represented the same company. This
   resolved 305 of 367 candidates.
2. The team recorded the available fields from each retrievable,
   identity-confirmed LinkedIn page. Five pages could not be retrieved, leaving
   300 companies.
3. All 300 included results passed the manual website-to-LinkedIn identity
   review, including independent verification of recent rebrands.
4. Five HTTP 404 results were excluded from evaluation but retained in the raw
   request manifest and response archive for auditability. The other 62 initial
   candidates remain outside v1 because their website-to-LinkedIn mapping was
   not resolved to the same standard.

The final slice distribution is:

| Slice | Final companies |
|---|---:|
| Stable large companies | 78 |
| Long-tail companies | 89 |
| Subsidiaries | 80 |
| Rebranded or domain-changed companies | 53 |
| **Total** | **300** |

The frozen company list and effective raw-response pointers are in
[`final-manifest.json`](../../data/firmographic/linkedin-live-ground-truth-v1/final-manifest.json).
The original 305-company attempt list remains in `manifest.json`; corrections
supersede original attempts for scoring without deleting the first response.
Raw manifests retain automated candidate-discovery metadata used to assemble
the human review queue; that metadata did not determine final inclusion.

## 3. Candidate selection is separate from reference data

The candidate frame answers only: "Which companies should be evaluated?"
It must not answer: "What is the correct value for each attribute?"

Rules:

1. OpenSearch `candidate_hints` are unverified and must not become expected
   answers merely because they came from the sampling source.
2. The Business Wire workbook establishes that a change event was announced;
   it is not reference data for all seven firmographic attributes.
3. Reference data must come from the manually verified company LinkedIn page,
   with recent rebrands checked against independent company or regulatory evidence.
4. Contradictions between the candidate frame and independent evidence must be
   resolved in favor of the evidence and logged.
5. Candidate identities and expected answers should be frozen before vendor
   evaluation begins.

## 4. OpenSearch candidate frame

### 4.1 Source and access

- Index: `companies_20260531`
- Access mode: authenticated, read-only search
- Sampler: [`sample_opensearch.py`](../../scripts/firmographic/sample_opensearch.py)
- Output: [`company-candidates-v1.json`](../../data/firmographic/company-candidates-v1.json)
- Master selection seed: `2026071301`
- Default oversample multiplier: `20`
- Candidate hash:
  `03a8ddc6b44aeabcf4acef2c1075eace6778054717792ad94b8de3ccef7c67ae`

The sampler uses fixed-seed OpenSearch `random_score`, with `id` as the random
field. Each stratum receives a deterministic derived seed computed from
`SHA-256(master_seed + ":" + stratum_name)`. Results are sorted by randomized
score and then company ID, so ties are deterministic.

Each query asks for:

```text
min(10,000, max(500, stratum quota × oversample multiplier))
```

hits before local eligibility, normalization, diversity, and deduplication are
applied.

### 4.2 Base eligibility

Every OpenSearch-sampled company must satisfy all of the following:

- `status = active`
- A `domains` field exists
- A `preferred_name` field exists
- A `location_country_code` field exists
- At least one domain can be normalized into a valid hostname

The first valid domain is used as the candidate input domain. Domains are
lowercased, a leading `www.` is removed, and path/query fragments are ignored.
Social-platform domains such as LinkedIn, Facebook, Instagram, TikTok, X,
Twitter, and YouTube are not eligible as company primary domains.

### 4.3 Global deduplication

The three OpenSearch slices are selected sequentially in this order:

1. Stable large
2. Long tail
3. Subsidiary

Across all three slices, both `company_id` and normalized input domain must be
unique. A company or domain selected in an earlier slice cannot be selected in
a later slice.

### 4.4 Stable large slice

Target: 100 companies.

Hard filters in addition to base eligibility:

- `employee_count_gte >= 1,000`
- `founded_on <= 2015-12-31`
- `is_subsidiary != true`

A soft country cap of 25 companies per country is applied (`quota / 4`). If
that cap would leave the slice short, the sampler fills the remaining positions
from the already-randomized eligible pool. The cap is therefore a diversity
goal, not a hard exclusion.

### 4.5 Long-tail slice

Target: 100 companies.

All long-tail candidates must have an employee-count interval fully inside the
intended small-company range:

- `employee_count_gte >= 10`
- `employee_count_lte <= 200`
- `is_subsidiary != true`

The slice is stratified as follows:

| Subslice | Quota | Additional filters | Soft country cap |
|---|---:|---|---:|
| U.S. English SMB | 40 | `location_country_code = USA`, `web_meta_lang = en` | None |
| Non-U.S. English SMB | 30 | `web_meta_lang = en`, country is not USA | 5 |
| Non-English SMB | 30 | Language exists and is not `en` | 3 |

As with stable large, country caps are relaxed only when required to complete a
subslice. Global company-ID and domain deduplication still applies.

### 4.6 Subsidiary slice

Target: 100 companies.

Hard filter in addition to base eligibility:

- `is_subsidiary = true`

A soft country cap of 20 companies per country is applied (`quota / 5`) and is
relaxed only to avoid a short slice.

Important: `is_subsidiary` is a source-data label used for candidate discovery.
It is not final proof of subsidiary status and is not used as a scored reference
field.

### 4.7 OpenSearch validation and freeze controls

The sampler refuses to write a manifest unless:

- Each of the three slices contains exactly 100 companies
- All 300 normalized domains are unique
- All 300 company IDs are unique
- Selected and target totals equal 300 and 500 respectively
- The stored cohort SHA-256 matches the ordered company identities

Candidate hints retained for later research include display name, location,
founded date, industry, LinkedIn slug, employee-count bounds, status, stage,
subsidiary flag, web language, and source vendor date. They remain unverified.
The source status is retained only because it was used for candidate-frame
eligibility; company operating status is not a scored benchmark attribute.

No accelerator field is part of the verified OpenSearch source contract used
by this sampler, so accelerator membership is not a slice or filter.

## 5. Rebrand and domain-change slice

### 5.1 Candidate source

The initial frame contained 100 rows compiled from the Business Wire Company
Name Change feed:

- Feed: <https://www.businesswire.com/newsroom/subject/company-name-change>
- Source workbook: `companies_rebranded_last_12_months.xlsx`
- Announcement dates in the workbook: 2025-07-16 through 2026-07-10
- All 100 rows had unique dated Business Wire article URLs

The original workbook is retained outside this repository and is never modified
by the curation script.

### 5.2 Structural acceptance rules

A row is eligible for verification only when all of the following are true:

- Change type is one of:
  - `Legal/company name`
  - `Brand/trading name`
  - `Corporate brand`
- Website outcome is one of:
  - `Domain changed / new site`
  - `Same / legacy domain retained`
- Exactly one old URL and one current URL are known
- Both URLs normalize to registrable domains
- The entity belongs in the company cohort rather than being a nonprofit

Multi-company consolidations, mergers, acquired business brands, divisional or
product-only changes, carve-outs, partial histories, and unconfirmed domain
transitions are rejected rather than forced into a one-company evaluation row.

### 5.3 Automated verification

Curator: [`curate_rebrand_workbook.py`](../../scripts/firmographic/curate_rebrand_workbook.py)

For every structurally eligible row, the script checks:

1. The source is a valid dated Business Wire per-article permalink.
2. The current company site is reachable.
3. Redirects stay on the expected current registrable domain.
4. The current site's HTML title, metadata, or visible text contains the new
   company identity. The matcher first tries the normalized full name and then
   requires at least two-thirds of meaningful name tokens.
5. The old site is fetched and its final URL/domain is recorded for audit,
   although an old site does not need to remain live.

Business Wire returned HTTP 403 to the automated client because of bot
protection. A 403 is treated only as a source-access limitation when the URL is
a syntactically valid dated Business Wire article permalink. It does not by
itself prove the rebrand; company or regulatory evidence is used for cases that
require manual resolution.

### 5.4 Manual verification

Transient bot blocks, JavaScript-only sites, and incorrect legacy domains are
resolved through explicit manual decisions. Every manual decision must include:

- Source row number
- Accepted/rejected decision
- Written reason
- One or more HTTPS evidence URLs
- Review date and reviewer
- A corrected current URL when necessary

Manual evidence file:
[`rebrand-manual-verifications-v1.json`](../../data/firmographic/rebrand-manual-verifications-v1.json)

Eighteen rows received explicit manual decisions: 17 accepted and one rejected.
The rejected row was Kids Kicking Cancer DBA MATIO, a 501(c)(3) outside the
company cohort. Two current-domain corrections were made from official company
evidence:

- Trigon Metals -> Safi Silver: `safisilver.com`
- Innovative Solutions & Support -> Innovative Aerosystems: `iascorp.com`

Manual review cannot turn a structurally rejected multi-entity or ambiguous row
into an accepted row. It can resolve only a verification failure for an
otherwise eligible one-to-one transition.

### 5.5 Rebrand result

- Accepted: 67
- Rejected: 33
- Unresolved review: 0
- Target remaining: 33
- Accepted-candidate hash:
  `2ad8a2163e19778f1680b6d5d0ba601a26c853bba0fc238f54d08a5f6aea32c5`

The 33 rejections consist of:

- 22 ambiguous, multi-entity, acquired-brand, division, product, consolidation,
  merger, or carve-out transitions
- 9 partial or unconfirmed website histories
- 2 nonprofits outside the company cohort

The accepted manifest is
[`rebrand-candidates-v1.json`](../../data/firmographic/rebrand-candidates-v1.json).
The row-by-row checks and rejection reasons are in
[`rebrand-verification-audit-v1.json`](../../data/firmographic/rebrand-verification-audit-v1.json).

The remaining 33 positions must come from separately sourced and equally
verified rebrands. Rejected rows must not be restored merely to reach 100.

## 6. Why the acquired/shut-down slice is not sampled from OpenSearch

The current OpenSearch frame is biased toward active, current company records
and the base sampler explicitly requires `status = active`. It does not provide
a sufficiently authoritative event history for acquisition dates, shutdowns,
or the distinction between a defunct company and a discontinued brand.

This slice therefore remains reserved. It should be built from external event
sources and independently confirm:

- The exact legal entity
- The event type and effective date
- Whether the company was acquired, absorbed, shut down, or merely rebranded
- The old and current domain behavior
- The operating status as of the benchmark cutoff date

## 7. Final LinkedIn reference data

The OpenBenchmarks team manually visited every candidate website and LinkedIn
company page and confirmed that each accepted pair represented the same company.
The active 300-company benchmark uses the available LinkedIn fields recorded for
each accepted, retrievable page. Automated mappings and provider agreement were
discovery aids only; they did not determine final acceptance. Effective response
pointers are recorded in `linkedin-live-ground-truth-v1/final-manifest.json`.

The deterministic field mapping and availability are:

| Benchmark field | LinkedIn extraction field | Available cases | Rule |
|---|---|---:|---|
| Company name | `company_name` | 300 | Current LinkedIn page name; not a guarantee of registered legal-entity wording |
| Primary domain | `domain`, then `website` | 299 | Normalize host and remove `www.`; do not fall back to the benchmark input |
| HQ country | `hq_country` | 289 | Compare country names and ISO codes canonically |
| HQ city | `hq_city` | 286 | Normalize case, punctuation, and whitespace |
| Founded year | `year_founded` | 220 | Parse a four-digit year from 1700–2099 |
| Industry | `industries` | 299 | Normalized exact or contained-label comparison |
| LinkedIn URL | `linkedin_url` | 300 | Normalize LinkedIn host, path, case, and trailing slash |
| Headcount band | `employee_range` | 297 | Compare canonical LinkedIn-style bands; three literal `None-None` values are missing reference data |
| Exact headcount | `employee_count` | 299 | Used only when a provider returns an exact count: pass within ±5% of the LinkedIn count |
| Funding stage | `funding_info.last_funding_round_type` | 0 | Excluded from every reference denominator |

The extractor returned no funding data of any kind for the final cohort: round
type, amount, currency, month, year, number of rounds, and Crunchbase URL were
all empty across all 300 responses. `employee_count` is used only to assess an
exact provider count against a ±5% tolerance; it is not used to infer a band
when `employee_range` is missing, because associated-member counts can conflict
with the LinkedIn size band.

### 7.1 Correctness comparison contract

The published v2 policy is reproducibly materialized from the immutable raw
LinkedIn responses and the versioned
[`identity-redirects-v1.json`](../../data/firmographic/identity-redirects-v1.json)
cache with:

```bash
npm run firmographic:refresh-v2-policy
```

This performs no provider, RapidAPI, or LLM request; it only replaces the
deterministic identity and headcount judgments and recomputes aggregates.

Only attributes with non-null reference data for that company enter its scoring
denominator. The published v2 release was rejudged field by field with
`gpt-5.6-terra` at medium reasoning effort through the OpenAI Responses API and
a strict structured-output
schema. The judge receives only the supplied LinkedIn reference value and the
provider value for each evaluable field. It is instructed to compare meaning,
remain conservative about company identity, and not replace the supplied
reference with outside model knowledge.

| Attribute | Correctness rule |
|---|---|
| Company name | Accept capitalization, punctuation, transliteration, word order, common abbreviations, and legal-suffix differences only when company identity remains clear; reject a parent, subsidiary, or similarly named entity. |
| Primary domain | Ignore scheme, `www.`, path, query, case, and trailing dots. Resolve an audited official redirect before comparing; retain the original input for audit. Inputs confirmed unreachable with no verified successor are excluded from this field's accuracy denominator and labeled inactive. |
| HQ location | Compare every supplied reference component; accept country names/codes, conventional city aliases, accents, and transliterations. |
| Founded year | Require the exact four-digit year. |
| Industry | A dedicated industry judge accepts exact/synonym labels plus broader umbrella and narrower subsector labels when they remain consistent with the same primary industry family. It rejects unrelated industries and contradictory specific subsectors. |
| LinkedIn URL | Ignore scheme, host prefix, case, query, and trailing slash; resolve an audited company-page redirect before requiring the same identity. |
| Headcount | Pass when the provider returns the same canonical LinkedIn band, treating `1–10` as equivalent to LinkedIn's `2–10` small-company band, or when its exact employee count is within ±5% of LinkedIn's exact count. Other overlapping broad ranges do not match. |

An absent provider value is always `provider_present=false` and
`is_correct=false`. Every verdict stores the reference, provider value, yes/no
decision, confidence, short rationale, and the former deterministic verdict.
The response validator rejects a batch if it omits or duplicates a company or
field, changes the provider-presence flag, or marks an absent value correct.

The final v2 run contains 14,014 field judgments across 2,100 provider-company
cells. It used 42 50-company chunks (six per provider); one validation retry
completed successfully. Successful chunks are immutable resume checkpoints, and
the manifest records the exact model, reasoning effort, prompt hash, source
snapshot hash, and token usage.

Auditable artifacts are stored under
[`llm-judge-v3`](../../data/firmographic/llm-judge-v3). The previous
deterministic metrics remain attached to each run under `deterministic_*` names
so the semantic judge can be compared with the reproducible baseline. The v3
prompt applies the exact-count condition (±5%), `1–10` ≈ `2–10`, audited domain
and LinkedIn redirects, and inactive-input primary-domain exclusion before it
returns the final structured verdict.

### 7.2 Dedicated industry adjudication

The post-run audit found that the general judge was stricter about industry
taxonomy granularity than the intended benchmark contract. All 2,093 non-null
industry references were therefore rejudged with a separate `gpt-5.6` prompt,
not only the first-pass mismatches. The dedicated contract explicitly treats
an umbrella category and a consistent subsector as a match. For example,
Software Development matches IT Services, Home Health Care matches Hospitals
and Health Care, and Truck Transportation matches Logistics. Explicitly
conflicting specific sectors, such as Higher Education and K-12 Schools, remain
mismatches.

The dedicated pass changed 105 raw first-pass industry verdicts. A random audit
then identified three direct applications of the written policy that the model
handled inconsistently: one Higher Education/K-12 conflict and two Primary and
Secondary Education/Education Administration mappings. These three applied
policy overrides are enumerated in the judge source, recorded in metric detail,
and leave the raw LLM output immutable.

Seven provider-level batches were attempted concurrently. Six validated
directly. CompanyEnrich's large response corrupted one opaque case hash, so it
was recovered as six 50-company checkpoints; only its final failed 49-case
checkpoint was retried. The final set contains all 2,093 expected judgments,
with no missing providers or cases. Artifacts are stored under
[`llm-industry-judge-v1`](../../data/firmographic/llm-industry-judge-v1).
The general judge's industry verdict and aggregate metrics remain available as
`general_llm_*`, and the deterministic baseline remains available as
`deterministic_*`.

For each provider/company cell, **correct field yield** is correct fields divided
by all available LinkedIn reference fields. **Accuracy when present** is correct
fields divided only by available reference fields that the provider returned.
Provider **coverage** is field presence across the seven active attributes and
does not include deferred funding stage.

The headline **correct field yield** is the number of correct provider fields
divided by all reference fields available for that company. **Accuracy when
present** divides correct fields by evaluable fields the provider returned.
Missing reference values are excluded from both denominators. These metrics are
dependent on the verified LinkedIn extraction as the sole field-level reference
and on one model judgment per field; they are not multi-annotator consensus.

Apply or validate this reference without vendor calls:

```bash
npm run firmographic:import-clay:dry
npm run firmographic:import-clay:staging
```

Inspect or apply the completed LLM judge checkpoints without paid calls:

```bash
npm run firmographic:judge:dry
npm run firmographic:judge:apply
npm run firmographic:judge-industry:dry
npm run firmographic:judge-industry:apply
```

## 8. Future adjudication requirements

The current LinkedIn-backed reference set is complete for this 300-company
version. A future multi-source or multi-annotator release should add:

- A canonical input domain
- Expected values for all seven scored attributes
- An explicit as-of date
- Field-level authoritative evidence URLs
- A confidence or adjudication status for conflicting sources
- Independent verification of subsidiary and operating-status labels

No candidate should be replaced after vendor evaluation begins. If a pre-run
candidate must be replaced, log the old identity, replacement identity, reason,
date, and new cohort hash.

## 9. Execution isolation and secrets

- Sampling and curation commands are read-only against source systems.
- Benchmark database work must use the Supabase staging branch, never the
  production branch.
- Runtime guards compare the configured Supabase URL with the expected staging
  project reference before creating a client.
- Credentials belong only in ignored local environment files and must never be
  written into manifests, methodology documents, or source control.

## 10. Reproduction and tests

Create the local Python environment once:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Run the OpenSearch sampler with the read-only source credentials:

```bash
npm run firmographic:sample -- --source-env ../self-serve-backend/.env
```

Re-run rebrand verification without modifying the source workbook:

```bash
npm run firmographic:curate-rebrands -- \
  /path/to/companies_rebranded_last_12_months.xlsx
```

Run the focused tests:

```bash
npm run firmographic:test
npm run firmographic:test-rebrands
```

At the time of this freeze, the OpenSearch sampler tests passed 5/5 and the
rebrand curator tests passed 8/8.

## 11. Known limitations

- OpenSearch source fields can be stale or wrong; they are sampling hints.
- LinkedIn company-page names are current display-name proxies, not guaranteed
  registered legal-entity names.
- Funding stage is unscored because the live source supplied no funding values
  for any of the 300 final companies.
- Subsidiary membership still needs independent ground-truth confirmation.
- The active-only frame cannot represent shut-down companies.
- Website reachability and redirects can change after verification.
- Business Wire bot protection prevents direct automated content confirmation.
- The rebrand slice is intentionally short at 67 until 33 more companies meet
  the same evidence standard.
