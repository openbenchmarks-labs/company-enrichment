# How these results were produced

## The run

Every one of the 282 cases in `data/latest-firmographic.json` was sent to Pvalyou the way a
customer request is: the company's domain and name go into the intake, which either finds the
company already on file or reads it live (its website, its own LinkedIn page, public registries,
third-party pages) and writes it to the catalogue. The record returned is the same record a paid
call returns. The adapter in `scripts/firmographic/providers/pvalyou.py` is that call; the runner
that produced `ours.json` drives the same path.

Nothing was tuned per case. Where a case exposed a weakness, the fix went into the pipeline as a
general rule (for example: a LinkedIn page's canonical address is kept beside the slug the
company links; a page whose stated website is another domain is another company's; a company
whose page states no headquarters line answers its primary office). All of it runs for every
company we serve.

## The judging

The five fields the board ranks, graded with the prompts and models published in this
repository, called through OpenRouter:

| field | judge | prompt |
|---|---|---|
| hq_location, founded_year, industry, linkedin_url | `openai/gpt-5.6-terra` | `firmographic-dedicated-field-judge-v1.2` |
| headcount_band | `openai/gpt-5.6-sol` | the numeric headcount policy |

`judged.json` holds every verdict with the judge's rationale, keyed by field then case.
`judge/bench_judge.py` re-grades `ours.json`; it needs an OpenRouter key.

## The numbers, by the board's definitions

| metric | value |
|---|---|
| enrichment success rate | 98.76% |
| field accuracy | 98.95% (1,314 correct of 1,328 returned) |
| field coverage | 99.92% (1,328 returned of 1,329 available) |
| resolution | 100% (282 of 282) |

Per field: founded_year 100.0%, hq_location 99.6%, headcount_band 98.9%, industry 98.6%,
linkedin_url 97.5%.

## Where we disagree with the reference

Fifteen answers. Each is listed in `reference_notes.md` with what the page says today.
