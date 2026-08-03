# Company Enrichment Benchmark

Open data and reproducible runners and judge code for the
[OpenBenchmarks Company Enrichment Benchmark](https://openbenchmarks.com/company-enrichment).

## Current release

The frozen release evaluates eight APIs on the same 282 reachable company
domains: 71 stable-large companies, 88 long-tail companies, 71 subsidiaries,
and 52 verified rebrands or domain changes. It contains 2,256 final
company-provider cells.

Every Ground Truth field was refreshed and manually reviewed across official
company sources, filings, registries, news, reputable reference sources,
redirects, and canonical or alternate LinkedIn URLs. Candidate-source values
only selected the cohort; they never became expected answers.

## What is scored

All providers receive the same input domain. The scored fields are:

- Headquarters country and city
- Founded year
- Industry
- LinkedIn company URL
- Employee band

Company name and primary domain remain in the data for identity audit, but are
not part of the scored denominator. The headline metric, **correct field
yield**, is the share of available Ground Truth fields correctly returned for a
company; missing and incorrect provider values lower the score, while a blank
Ground Truth field is excluded.

Employee-band evaluation accepts exact-count containment and the documented
five-percent tolerance when both values are exact counts, as well as narrowly
off-by-one finite ranges. It is rejudged with the dedicated `gpt-5.6-sol`
medium-reasoning numeric policy; the other semantic fields use `gpt-5.6-terra`.
See `scripts/field_judge_prompts.py` for the complete versioned policy.

## Active providers

- Apollo
- CompanyEnrich
- Exa
- Explorium
- Parallel
- People Data Labs
- Predict Leads
- ZoomInfo

Fiber and Ocean.io are retained only as historical adapters; they are not in
this snapshot.

## Repository map

| Path | Contents |
|---|---|
| `data/firmographic/company-inputs-v2.csv` | Exact frozen 282-company input list sent to every provider |
| `data/firmographic/company-ground-truth-v2.json` | Compact human-reviewed Ground Truth reference |
| `data/latest-firmographic.json` | Published snapshot: normalized provider responses, field decisions, and leaderboard |
| `scripts/run_firmographic_full_benchmark.py` | Resumable runners for the standard enrichment APIs |
| `scripts/run_web_research_benchmark.py` | Resumable Exa and Parallel web-research runners |
| `scripts/firmographic/providers/` | Provider adapters and shared normalized response contract |
| `scripts/field_judge_prompts.py` | Versioned Terra semantic-field and Sol employee-band judge policies |
| `scripts/run_dedicated_field_judges.py` | Checkpointed per-provider, per-field judge and snapshot applicator |
| `scripts/firmographic/run_zoominfo_enrichment.py` | Resumable ZoomInfo GTM CLI company-enrichment runner |
| `scripts/firmographic/rejudge_headcount_sol.py` | Dedicated Sol medium employee-band rejudge runner |
| `scripts/export_firmographic_v2_artifacts.py` | Rebuilds the compact public inputs and Ground Truth files from the snapshot |
| `scripts/verify_public_artifacts.py` | Offline integrity and coverage verification |

See [DATA.md](DATA.md) and [the methodology](docs/company-enrichment/company-enrichment-sampling-method.md)
for the data contract and cohort construction details.

## Verify without API calls

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
python3 scripts/export_firmographic_v2_artifacts.py
python3 scripts/verify_public_artifacts.py
```

## Re-run live APIs

Copy `.env.example` to `.env.local` and configure only the provider(s) you
intend to call. Every live runner requires `--confirm-paid` and resumes from
successful checkpoints by default.

```bash
# Exa and Parallel use the same domain-only web-research contract.
PYTHONPATH=scripts .venv/bin/python scripts/run_web_research_benchmark.py \
  --only parallel-research,exa-research-v2 --confirm-paid

# Standard provider adapter example.
PYTHONPATH=scripts .venv/bin/python scripts/run_firmographic_full_benchmark.py \
  --only apollo --confirm-paid

# ZoomInfo uses its GTM CLI with the same domain-only field contract.
PYTHONPATH=scripts .venv/bin/python scripts/firmographic/run_zoominfo_enrichment.py \
  --run
```

To rejudge after a new run, configure `OPENAI_API_KEY` and use the dedicated
field judge. It sends one provider/field batch at a time, writes checkpoints,
and applies results only with `--apply`.

```bash
PYTHONPATH=scripts .venv/bin/python scripts/run_dedicated_field_judges.py \
  --confirm-paid --apply
```

The snapshot includes normalized answers, request/response audit metadata,
latency, usage, and the final per-field judge rationales. It excludes secrets
and labeller review state.

No vendor sponsors or controls this benchmark.
