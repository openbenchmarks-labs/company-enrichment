# Company enrichment benchmark methodology

This document is the reproducible form of the methodology published at
<https://openbenchmarks.com/company-enrichment>.

## 1. Cohort design

The final cohort contains four slices that test different enrichment failure
modes:

| Slice | Final count | Purpose |
|---|---:|---|
| Stable large | 78 | Active, non-subsidiary companies with at least 1,000 employees and founded by 2015; the well-documented baseline |
| Long tail | 89 | Active, non-subsidiary companies with 10–200 employees across U.S. English, non-U.S. English, and non-English strata; sparse and international coverage |
| Subsidiaries | 80 | Companies marked as subsidiaries in the discovery index; parent-versus-requested-entity confusion |
| Verified rebrands | 53 | Recent one-company name or domain changes; stale identity records |
| **Total** | **300** | |

The source subsidiary flag selected candidates but was not used as reference
data or as a scored field.

## 2. Sampling and freeze

### Stable-large, long-tail, and subsidiary slices

The discovery frame was an active-company index. Candidate attributes were
used only for selection and never as ground truth or scored answers.

- Master seed: `2026071301`
- Randomization: deterministic random scoring with a per-stratum seed
  derived from `SHA-256(master_seed + ":" + stratum_name)`
- Tie-break: company ID
- Global constraints: unique company ID and normalized domain across all slices
- Base eligibility: active record with a company name, country, and valid domain
- Social-network domains are not eligible as primary domains

Stable-large candidates require at least 1,000 employees, a founding date on or
before 2015-12-31, and no subsidiary flag. A soft cap of 25 companies per
country promotes geographic diversity.

Long-tail candidates require an employee interval fully inside 10–200 and no
subsidiary flag. The target is stratified into 40 U.S. English, 30 non-U.S.
English, and 30 non-English companies, with soft country caps of 5 and 3 in the
latter two strata.

Subsidiary candidates require `is_subsidiary = true`, with a soft cap of 20 per
country. Soft caps may relax only to fill the fixed quota.

The candidate frame required exactly 100 unique companies and domains in each
of these three slices before identity review.

### Rebrand slice

One hundred recent rows were reviewed from Business Wire's maintained
[Company Name Change feed](https://www.businesswire.com/newsroom/subject/company-name-change).
The reviewed announcements covered 2025-07-16 through 2026-07-10.

Only unambiguous one-company legal, brand, or corporate-name transitions with a
verified current identity were accepted. Mergers, consolidations, divisions,
product-only changes, carve-outs, partial histories, unconfirmed domain changes,
and nonprofits were rejected. Sixty-seven rows passed candidate review.

The 367-candidate frame was frozen before any provider was scored. Every vendor
received the same final 300 domains.

## 3. Identity and reference data

The OpenBenchmarks team visited every candidate's website and LinkedIn company
page and confirmed that the pair represented the same entity. This resolved 305
of 367 candidates. The team recorded the available benchmark fields from each
identity-confirmed LinkedIn page; five pages were not retrievable, leaving the
frozen 300-company cohort.

The exact pairs and fields are in `company-inputs-v1.csv` and
`company-ground-truth-v1.json`. Candidate-index values never become expected
answers.

## 4. Provider execution

Each provider receives the company domain. The seven adapters normalize results
to company name, primary domain, headquarters country/city, founded year,
industry, LinkedIn URL, and headcount band.

The runner records one cell per company/provider with normalized values, status,
latency, usage, errors, and audit metadata. It checkpoints cells independently,
skips existing cells by default, runs at most one request chain per provider at
a time, and requires explicit confirmation before paid calls.

## 5. Judging

Only non-null reference fields are judged. The general field pass uses GPT-5.6
with high reasoning and strict structured output. Fiber ran as one 300-company
call; the other six providers ran as six 50-company chunks each for safer
checkpointing. The field rubric and structured schema were unchanged across
providers.

The judge compares meaning rather than formatting while preserving company
identity. It accepts conventional name suffix/punctuation differences, canonical
domain and LinkedIn formatting, country codes, city aliases, and exact employee
counts inside the reference band. Founded year must match exactly; a broad
headcount band that merely overlaps does not match.

Industry receives a dedicated second pass so the same rule is applied
consistently: established taxonomy synonyms and genuinely equivalent broader or
narrower labels are accepted; adjacent sectors and labels too broad to preserve
the company's primary business are rejected.

Every final field decision and rationale is committed. Results were spot-checked
by a human.

## 6. Metrics

For one company/provider cell:

```text
correct field yield = correct fields / available reference fields
accuracy when present = correct fields / returned evaluable fields
field coverage = returned scored fields / 7
```

Missing or incorrect provider values lower correct field yield. Missing
reference values are excluded. Provider leaderboard values are the mean of the
300 company-level percentages.

Resolution is the share of domains for which the adapter returned a normalized
company. Median latency is computed from recorded cell latency. Estimated USD
cost multiplies recorded usage by the public entry paid self-serve rate dated
July 14, 2026; free allowances, taxes, minimum commitments, and volume discounts
are excluded.

## 7. Reproduction

Verify the committed data without network calls:

```bash
python3 scripts/verify_public_artifacts.py
```

Recompute the snapshot from local normalized cells and reference data:

```bash
PYTHONPATH=scripts python3 scripts/recompute_firmographic_snapshot.py
```

Apply the committed judge results without issuing new judge calls:

```bash
PYTHONPATH=scripts python3 scripts/judge_firmographic_with_openai.py --apply
PYTHONPATH=scripts python3 scripts/judge_firmographic_industry_with_openai.py --apply
```

Live vendor or judge runs require credentials and explicit paid-call flags; they
are unnecessary to audit the published scores.
