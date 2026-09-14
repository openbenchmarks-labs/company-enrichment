"""Run the benchmark's OWN field judges over our output, through OpenRouter.

WHY (2026-09-12). bench_score is deterministic and therefore a FLOOR. The leaderboard's numbers come
from the benchmark's own `gpt-5.6-terra` field judges at medium reasoning, whose published prompts
are materially more lenient than rule comparison in specific ways this repo's scorer cannot imitate
without guessing:

  headcount   "Any provider range, including min=max, is correct when the reference exact_count
              falls inside it." Butte College is banded 201-500 with an exact count of 1,205, so our
              (1001, 5000) is CORRECT under the judge and wrong under a band comparison.
  hq_location accents, transliterations, conventional city aliases, a specific locality inside the
              reference metro, England/UK/GB equivalence, New Delhi/Gurgaon equivalence.
  industry    synonyms, a broader category containing the reference, a narrower specialisation
              inside it, or an adjacent taxonomy label for the same primary business.

Measured on their own published runs, the judge reads 1 to 18 points ABOVE a rule comparison
depending on the provider. So the only way to know where we stand on their board is to run their
judge, and the only honest way to do that is with THEIR prompts, THEIR model and THEIR schema.

⚠ NO OPENAI KEY NEEDED, AND NOTHING OF THEIRS IS REWRITTEN. OpenRouter serves both judge models
(openai/gpt-5.6-terra, openai/gpt-5.6-sol) AND exposes OpenAI's Responses API at /api/v1/responses
— verified by probe, and verified end to end with their exact `responses.parse(...)` call including
the strict Pydantic schema. The ONLY changes are the base_url, our own key, and the `openai/` model
prefix. Their PROMPTS, PROMPT_VERSION, reasoning effort, schema and their check that every case
comes back are imported from their repo and untouched, because the whole value of running their
judge is that it is theirs.

⚠ IT IS ALSO A SECOND OPINION ON OUR OWN SCORER. Pass --providers to judge THEIR published provider
answers with the same call, and the result should reproduce their published leaderboard. If it does
not, the judging setup is wrong and our own number from it means nothing.

    python tools/benchmark/bench_judge.py --ours scratch/benchmark/ours_282.json
    python tools/benchmark/bench_judge.py --ours F --field headcount_band     # one field
    python tools/benchmark/bench_judge.py --providers people-data-labs        # validate the setup
"""
import argparse
import json
import sys
import time
from pathlib import Path

import os

# standalone copy for results/pvalyou/judge: the repository root is three levels up, the
# OpenRouter key comes from OPENROUTER_API_KEY, and nothing is traced
BENCH = Path(__file__).resolve().parents[3]
RESULTS = BENCH / "data" / "latest-firmographic.json"
# their scripts/ must be importable so the PROMPTS come from their file, never a copy
sys.path.insert(0, str(BENCH / "scripts"))

RANKED = ("hq_location", "founded_year", "industry", "linkedin_url", "headcount_band")


def _client():
    from openai import OpenAI
    return OpenAI(api_key=os.environ["OPENROUTER_API_KEY"],
                  base_url="https://openrouter.ai/api/v1", max_retries=1, timeout=900)


# ⚠ THE JUDGE COSTS REAL MONEY AND WAS REPORTING NONE OF IT (2026-09-13). Ten calls per pass over
# 282 cases at medium reasoning is the most expensive thing in this directory, and it appeared in no
# trace, no bucket and no dashboard — exactly the class test_guards' blind-file ratchet exists to
# stop, and it caught this one. It cannot use eval_batches.post_job_llm, which speaks chat
# completions: the whole point of this file is to call the SAME Responses API their runner calls, so
# the measurement is theirs rather than a re-implementation. So it opens its own root and attaches
# each call as a generation with the provider's own usage.
#
# ⚠ AND THE FIRST TWO VERSIONS EACH GOT IT WRONG IN THEIR OWN WAY (2026-09-13): one indexed the
# root as a dict, the next wrote a bare {"total": x} that lf_attach's tally does not count, so
# the cost column stayed blank while OpenRouter had returned a cost on every call. bench_trace now
# owns the root, the session, the priced generation and the cost scores, the way the pipeline
# does them, for this judge and the funding one alike.
_ROOT = None
_STATE = None
_ANSWERS = None        # the answers file being judged; the session id is derived from it


def _root():
    return None    # no tracing in the standalone copy


def _report(field, model, label, start, resp, n_rows):
    """One judge call, on the trace, priced like a pipeline LLM call. Never raises."""
    return None    # no tracing in the standalone copy


def value(norm, f):
    """THEIR val(): the provider-side value for one field, in the shape their prompt expects."""
    if not norm:
        return None
    if f == "hq_location":
        v = {"country": norm.get("hq_country"), "city": norm.get("hq_city")}
        return v if any(v.values()) else None
    if f == "headcount_band":
        v = {"min": norm.get("headcount_min"), "max": norm.get("headcount_max")}
        return v if any(x is not None for x in v.values()) else None
    if f == "industry":
        return [x for x in [norm.get("industry"), *(norm.get("industries") or [])] if x] or None
    if f == "primary_domain":
        return [x for x in [norm.get("primary_domain"), *(norm.get("domains") or [])] if x] or None
    return norm.get(f)


def rows_for(cases, answers, f):
    """THEIR row shape, one per case whose reference has this field."""
    out = []
    for c in cases:
        ref = value(c["reference"], f)
        if ref in (None, [], {}):
            continue
        if f == "linkedin_url":
            alts = c["reference"].get("linkedin_url_alternates") or []
            if isinstance(alts, str):
                alts = [x.strip() for x in alts.replace("\n", ",").split(",") if x.strip()]
            ref = [x for x in [c["reference"].get("linkedin_url"), *alts] if x]
        out.append({"case_slug": c["case_slug"], "reference": ref,
                    "provider_value": value(answers.get(c["case_slug"]), f),
                    "alternate_primary_domains":
                        c["reference"].get("alternate_primary_domains", []),
                    "reference_exact_headcount": c["reference"].get("headcount_exact")})
    return out


def judge(field, rows, label):
    """One judge call. Returns {case_slug: (is_correct, rationale)}."""
    from pydantic import BaseModel
    from field_judge_prompts import (MODEL, REASONING_EFFORT, PROMPTS, PROMPT_VERSION,
                                     HEADCOUNT_SOL_PROMPT)

    class Verdict(BaseModel):
        case_slug: str
        provider_present: bool
        is_correct: bool
        rationale: str

    class Output(BaseModel):
        provider_slug: str
        verdicts: list[Verdict]

    # ⚠ HEADCOUNT USES THE SOL JUDGE, AS THEIR README SAYS: "It is rejudged with the dedicated
    # gpt-5.6-sol medium-reasoning numeric policy; the other semantic fields use gpt-5.6-terra."
    # Judging headcount with terra would be a different measurement from the published one.
    model, prompt = ((("openai/gpt-5.6-sol"), HEADCOUNT_SOL_PROMPT)
                     if field == "headcount_band" else (f"openai/{MODEL}", PROMPTS[field]))
    _t0 = time.time()
    r = _client().responses.parse(
        model=model, reasoning={"effort": REASONING_EFFORT},
        input=[{"role": "system", "content": prompt},
               {"role": "user", "content": json.dumps(
                   {"provider_slug": label, "field": field, "cases": rows},
                   separators=(",", ":"))}],
        text_format=Output, max_output_tokens=50000, store=False)
    _report(field, model, label, _t0, r, len(rows))
    out = r.output_parsed
    if not out:
        raise ValueError(f"{field}: no parsed output")
    got = {v.case_slug for v in out.verdicts}
    want = {x["case_slug"] for x in rows}
    if got != want:
        # their own runner raises here too — a judge that skipped cases is not a measurement
        raise ValueError(f"{field}: judge returned {len(got)} of {len(want)} cases "
                         f"(missing {sorted(want - got)[:4]})")
    return {v.case_slug: (v.is_correct, v.rationale) for v in out.verdicts}, PROMPT_VERSION, model


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ours", help="our output file, {case_slug: answer}")
    ap.add_argument("--providers", help="instead judge THIS published provider, to validate setup")
    ap.add_argument("--field", choices=RANKED, help="one field only")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent / "judged.json"))
    a = ap.parse_args()
    global _ANSWERS
    _ANSWERS = a.ours or a.providers        # the session id names the answers file being judged
    d = json.loads(RESULTS.read_text(encoding="utf-8"))
    cases = d["cases"]
    if a.providers:
        answers = {r["case_slug"]: r.get("normalized") for r in d["runs"]
                   if r["provider_slug"] == a.providers}
        label = a.providers
        if not answers:
            raise SystemExit(f"no runs for provider {a.providers!r}")
    else:
        if not a.ours:
            raise SystemExit("pass --ours or --providers")
        answers = json.loads(Path(a.ours).read_text(encoding="utf-8"))
        label = "pvalyou"
    fields = [a.field] if a.field else list(RANKED)
    print(f"judging {label} on {len(fields)} field(s) over {len(cases)} cases\n")
    per, saved = {}, {}
    for f in fields:
        rows = rows_for(cases, answers, f)
        try:
            verdicts, version, model = judge(f, rows, label)
        except Exception as e:
            print(f"  {f:<18}FAILED {type(e).__name__}: {str(e)[:150]}")
            continue
        ok = sum(1 for v, _r in verdicts.values() if v)
        per[f] = (ok, len(rows))
        saved[f] = {"model": model, "prompt_version": version,
                    "verdicts": {k: {"is_correct": v, "rationale": r}
                                 for k, (v, r) in verdicts.items()}}
        print(f"  {f:<18}{ok:>4}/{len(rows):<5}{100 * ok / len(rows):>7.1f}%   {model}")
        Path(a.out).write_text(json.dumps(saved, indent=1, ensure_ascii=False), encoding="utf-8")
    if len(per) < len(fields):
        # a field whose judge failed is printed above; a scripted run must not read a partial
        # file as a measurement, so the process says so in its exit status too
        print(f"\n  ⚠ {len(fields) - len(per)} field(s) NOT judged (see FAILED above): partial output")
    if len(per) == len(RANKED):
        # ⚠ FOUR METRICS, NOT ONE (2026-09-12, read off openbenchmarks.com). Their site ranks
        # providers on different numbers for different jobs, and computing only the headline hid
        # three of them:
        #   enrichment success rate  correct / every available reference field   (the headline)
        #   field accuracy           correct / the fields WE RETURNED            (wrong answers
        #                            are punished, blanks are not — "safe CRM writeback")
        #   field coverage           returned / available                        (completeness)
        #   resolution               companies we answered at all
        # A provider can lead one and trail another: Apollo leads accuracy at 92.6% while People
        # Data Labs leads success at 89.0% and Parallel leads coverage at 94.7%.
        by_case, returned, correct, available = {}, 0, 0, 0
        for f in RANKED:
            rows = {r["case_slug"]: r for r in rows_for(cases, answers, f)}
            for slug, ent in saved[f]["verdicts"].items():
                ok = 1 if ent["is_correct"] else 0
                by_case.setdefault(slug, []).append(ok)
                available += 1
                correct += ok
                if (rows.get(slug) or {}).get("provider_value") not in (None, [], {}, ""):
                    returned += 1
        yields = [100 * sum(v) / len(v) for v in by_case.values() if v]
        answered = sum(1 for s in {c["case_slug"] for c in cases}
                       if (answers.get(s) or {}) not in (None, {}))
        print(f"\n  enrichment success rate : {sum(yields) / len(yields):>6.2f}%   "
              f"(their headline; best provider People Data Labs 89.0)")
        print(f"  field accuracy          : {100 * correct / max(returned, 1):>6.2f}%   "
              f"({correct} correct of {returned} returned; best Apollo 92.6)")
        print(f"  field coverage          : {100 * returned / max(available, 1):>6.2f}%   "
              f"({returned} returned of {available} available; best Parallel 94.7)")
        print(f"  resolution              : {100 * answered / len(cases):>6.2f}%   "
              f"({answered} of {len(cases)} companies; best Predict Leads 100.0)")
    print(f"\nwrote {a.out}")


if __name__ == "__main__":
    main()
