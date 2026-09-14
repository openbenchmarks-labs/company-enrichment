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
| enrichment success rate | 98.00% |
| field accuracy | 98.27% (1,304 correct of 1,327 returned) |
| field coverage | 99.85% (1,327 returned of 1,329 available) |
| resolution | 100% (282 of 282) |

Per field: hq_location 99.6%, headcount_band 98.6%, industry 98.2%, founded_year 97.6%,
linkedin_url 96.5%.

These are the numbers of one pass: all 282 companies sent through the same code version on
2026-09-14, 280 of them read live that night, then judged once. Earlier passes that re-ran
subsets after each fix scored higher (98.76%); the single pass is the one we stand behind.

## Where we disagree with the reference

Twenty-three answers, plus two we do not give. Each is listed in `reference_notes.md` with what
the page says today.
