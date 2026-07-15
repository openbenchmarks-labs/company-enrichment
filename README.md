# Company Enrichment Benchmark

Open data and reproducible runner/judge code for the
[OpenBenchmarks Company Enrichment Benchmark](https://openbenchmarks.com/company-enrichment).

The frozen release compares seven company-enrichment APIs on the same 300
domains: 78 stable-large companies, 89 long-tail companies, 80 subsidiaries,
and 53 verified rebrands. It contains 2,100 final company-provider cells.

## Scored fields

Every provider response is normalized to the same seven-field contract:

- Company name
- Primary domain
- Headquarters country and city
- Founded year
- Industry
- LinkedIn company URL
- Headcount band

The headline metric is **correct field yield**: correct fields divided by the
reference fields available for that company. A missing or incorrect provider
value lowers the score; a missing reference value is excluded. The leaderboard
is the mean of the 300 company-level scores.

## Repository map

| Path | Contents |
|---|---|
| `data/firmographic/company-inputs-v1.csv` | Exact frozen 300-company input list and manually verified LinkedIn pairs |
| `data/firmographic/company-ground-truth-v1.json` | Available reference fields recorded from the 300 verified LinkedIn pages |
| `data/latest-firmographic.json` | Publication snapshot: inputs, 2,100 normalized provider outputs, judgments, and leaderboard |
| `data/firmographic/llm-judge-v2/` | General field-judge call checkpoints, aggregate results, usage, and rationales |
| `data/firmographic/llm-industry-judge-v1/` | Dedicated industry-judge checkpoints and final industry decisions |
| `data/firmographic/pricing-v1.json` | Dated public entry-tier cost assumptions used for estimated USD cost |
| `scripts/run_firmographic_full_benchmark.py` | Credit-safe, resumable provider runner |
| `scripts/firmographic/providers/` | Seven provider adapters and normalization logic |
| `scripts/judge_firmographic_*.py` | Structured-output judging and checkpoint application |
| `scripts/recompute_firmographic_snapshot.py` | Offline metric and leaderboard recomputation |
| `scripts/verify_public_artifacts.py` | Zero-network integrity and coverage checks |

See [DATA.md](DATA.md) for schemas and
[the methodology](docs/company-enrichment/company-enrichment-sampling-method.md)
for the complete sampling and evaluation contract.

## Verify without API calls

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
python3 scripts/verify_public_artifacts.py

PYTHONPATH=scripts .venv/bin/python -m unittest \
  firmographic.test_common \
  firmographic.test_provider_adapters \
  firmographic.test_full_runner
```

The verifier checks the frozen cohort, slice counts, 2,100-cell matrix, provider
aggregates, and judge coverage. It makes no network calls.

## Re-run live APIs

Copy `.env.example` to `.env.local` and configure only the providers you intend
to run. Live vendor calls require `--confirm-paid`. Existing checkpoints of
every status are skipped unless a retry status is explicitly selected.

```bash
PYTHONPATH=scripts .venv/bin/python scripts/run_firmographic_full_benchmark.py \
  --only fiber --confirm-paid
```

The committed snapshot contains every normalized provider answer used by the
benchmark, including status, latency, usage, errors, and adapter audit metadata.
The runner intentionally stores the normalized benchmark contract, not literal
full vendor HTTP response bodies.

## Providers

- Apollo
- CompanyEnrich
- Explorium
- Fiber
- Ocean.io
- People Data Labs
- PredictLeads

No vendor sponsors or controls this benchmark.
