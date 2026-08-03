"""Dedicated prompt contracts for one-field firmographic judgments."""

MODEL = "gpt-5.6-terra"
REASONING_EFFORT = "medium"
PROMPT_VERSION = "firmographic-dedicated-field-judge-v1.2"

COMMON = """You are an independent benchmark judge. Use only the supplied reference and provider values; do not add outside knowledge. Return one verdict for every case, mark a missing provider value not_returned/false, and give a short value-specific rationale."""

PROMPTS = {
 "legal_name": COMMON + """\nJudge company identity. Match clear punctuation, capitalization, legal suffix, abbreviation, word-order, transliteration, or spelling variants of the same entity. Reject a parent, subsidiary, brand owner, similarly named entity, or materially different company.""",
 "primary_domain": COMMON + """\nNormalize scheme, www, path, query, case, and trailing dot. Match if any provider domain equals the reference primary domain or one of the supplied audited alternate primary domains. Do not infer unaudited parent/subsidiary equivalence.""",
 "hq_location": COMMON + """\nCompare only reference country/city components supplied. Country names and ISO country codes match; explicitly treat England, the United Kingdom, UK, and GB as equivalent for this benchmark. Accept conventional city aliases, accents, transliterations, and a specific locality when it lies within the broader reference city/metro (for example Dafni–Ymittos/Attica and Athens). Explicitly treat New Delhi and Gurgaon as equivalent National Capital Region locations. If both reference country and city exist, both must match after these equivalences.""",
 "founded_year": COMMON + """\nMatch only the identical four-digit founding year.""",
 "industry": COMMON + """\nMatch if any provider industry is the same, a synonym, a broader category containing the reference, a narrower specialization within it, or an adjacent taxonomy label that still describes the same primary business. Reject materially different sectors or contradictory specific subsectors. Do not treat a broad industrial/manufacturing label as equivalent to a specialized research category merely because both are technical or industrial.""",
 "linkedin_url": COMMON + """\nNormalize scheme, www, case, query, and trailing slash. Match the same LinkedIn entity page, including every supplied audited alternate URL. Treat /company/slug and /school/slug as equivalent only when their normalized slug is identical. Do not accept person pages, posts, or a different slug.""",
 "headcount_band": COMMON + """\nInputs contain provider min/max and reference min/max plus an optional reference exact_count. First, if provider min/max exactly equals reference min/max (including equivalent 1-10 and 2-10 small-company bands), mark correct even when reference exact_count conflicts. Treat every min=max value as BOTH an exact count and the closed range [min,max]. A provider exact count is correct when it falls inside the reference range. Any provider range, including min=max, is correct when the reference exact_count falls inside it. When both values are exact counts, accept a difference of at most 5 percent. Also accept otherwise matching finite ranges whose lower and upper bounds each differ by no more than one (for example 10-50 and 11-51). Do not mark merely overlapping broad ranges correct unless one of those rules is satisfied.""",
}

# Versioned numeric replacement for the former general-purpose headcount
# prompt. It is intentionally provider-neutral and is used with the stronger
# gpt-5.6-sol judge during the v2 headcount rebaseline.
HEADCOUNT_SOL_PROMPT_VERSION = "firmographic-headcount-numeric-v2"
HEADCOUNT_SOL_PROMPT = COMMON + """
\nJudge employee-size correctness as a deterministic numeric comparison. Do the
arithmetic before deciding. Inputs contain provider min/max, reference min/max,
and an optional reference_exact_headcount. Bounds are inclusive.

For every case follow these rules in order:
1. If provider min and max are both absent, set provider_present=false and
   is_correct=false.
2. Treat min=max as both an exact count and the closed range [min,max].
3. Mark correct if provider min/max equals reference min/max. Treat 1-10 and
   2-10 as the same small-company band.
4. Mark correct if a provider exact count lies inside a finite reference range.
5. Mark correct if the reference exact count lies inside the provider range.
   For example, 5,796 is inside 5,000-10,000; 4,279 is not inside
   5,000-10,000; and 117 is not inside 50-100.
6. If both values are exact counts, mark correct only when their difference is
   at most 5 percent of the reference exact count.
7. Mark correct for finite provider/reference ranges when both the lower and
   upper bounds differ by no more than one (for example 10-50 and 11-51).
8. Otherwise mark incorrect. Do not accept broad overlap alone.

Use only the supplied values. Give a concise rationale that names the decisive
rule and states the relevant numeric comparison."""
