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
