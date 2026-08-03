"""Provider-neutral contract for web-research company-enrichment APIs.

Parallel and Exa have different transport envelopes, but receive the exact
same identity input, instruction, and output schema. Keep product-specific
request logic in provider adapters; changes to the research task belong here
so web-research providers remain comparable to one another.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

from firmographic.common import CompanyCase, NormalizedCompany


PROMPT_VERSION = "firmographic-web-research-v1.0"
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "legal_name": {"type": ["string", "null"]},
        "primary_domain": {"type": ["string", "null"]},
        "hq_country": {"type": ["string", "null"]},
        "hq_city": {"type": ["string", "null"]},
        "founded_year": {"type": ["integer", "null"]},
        "industry": {"type": ["string", "null"]},
        "industries": {"type": "array", "items": {"type": "string"}},
        "linkedin_url": {"type": ["string", "null"]},
        "headcount_min": {"type": ["integer", "null"]},
        "headcount_max": {"type": ["integer", "null"]},
        "sources": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "legal_name", "primary_domain", "hq_country", "hq_city",
        "founded_year", "industry", "industries", "linkedin_url",
        "headcount_min", "headcount_max", "sources",
    ],
    "additionalProperties": False,
}
# Exa's current structured-output endpoint accepts at most ten properties. The
# ten scored fields fit exactly; citations are returned separately in Exa's
# grounding envelope and restored into the canonical schema after decoding.
EXA_OUTPUT_SCHEMA: dict[str, Any] = {
    **OUTPUT_SCHEMA,
    "properties": {
        key: value for key, value in OUTPUT_SCHEMA["properties"].items() if key != "sources"
    },
    "required": [key for key in OUTPUT_SCHEMA["required"] if key != "sources"],
}
# Exa recommends concise, described schemas and handling behavioural guidance
# through `systemPrompt`. Its `/search` endpoint accepts at most ten schema
# properties, so citations remain in the native grounding envelope.
EXA_V2_OUTPUT_SCHEMA: dict[str, Any] = {
    **EXA_OUTPUT_SCHEMA,
    "properties": {
        "legal_name": {"type": ["string", "null"], "description": "Official company or legal name."},
        "primary_domain": {"type": ["string", "null"], "description": "Canonical public website domain only, without a URL path."},
        "hq_country": {"type": ["string", "null"], "description": "Country of the company's headquarters."},
        "hq_city": {"type": ["string", "null"], "description": "City of the company's headquarters."},
        "founded_year": {"type": ["integer", "null"], "description": "Four-digit year the company was founded."},
        "industry": {"type": ["string", "null"], "description": "Primary industry or business category."},
        "industries": {"type": "array", "items": {"type": "string"}, "description": "Up to four relevant industry labels."},
        "linkedin_url": {"type": ["string", "null"], "description": "Canonical LinkedIn company-page URL, not a person or post URL."},
        "headcount_min": {"type": ["integer", "null"], "description": "Lower bound of a supported employee-count range."},
        "headcount_max": {"type": ["integer", "null"], "description": "Upper bound of a supported employee-count range."},
    },
}
SCORING_FIELDS = tuple(key for key in OUTPUT_SCHEMA["properties"] if key != "sources")


def instruction(input_name: str | None, input_domain: str) -> str:
    """The invariant research instruction for every web-research provider."""
    return f"""Research the company identified by this website: {input_domain}.
The supplied company name is {input_name!r}. Return a company-enrichment record
for that exact company. Do not substitute a parent, subsidiary, similarly named
company, or brand owner unless the supplied website itself identifies that entity.

Use reliable first-party or LinkedIn sources where available. Return null for
unknown scalar values and [] for unknown lists; do not guess. For headcount,
return a range only when the source supports a range. Include the URLs used in
sources. Return only the supplied JSON-schema object."""


def exa_v2_query(input_name: str | None, input_domain: str) -> str:
    """Short retrieval query; Exa uses systemPrompt for synthesis behaviour."""
    name = input_name or input_domain
    return f'Company profile for "{name}" ({input_domain}).'


def exa_v2_system_prompt(input_name: str | None, input_domain: str) -> str:
    """Documented Exa system prompt for grounded company enrichment."""
    return f"""Research the exact company identified by supplied name {input_name!r}
and website {input_domain!r}. Prefer the company website and LinkedIn company
page, then reputable business sources. Extract every requested value that is
explicitly supported by the retrieved evidence. Do not substitute a parent,
subsidiary, similarly named company, or brand owner unless the supplied website
identifies that entity. Return null for unsupported scalar values and [] for
unsupported lists. For headcount, return a range only when supported. Never
invent values. Return only the object required by outputSchema."""


def request_contract(case: CompanyCase) -> dict[str, Any]:
    """Auditable, provider-independent input record (no reference values)."""
    return {
        "prompt_version": PROMPT_VERSION,
        "input": {"company_name": case.input_name, "company_domain": case.input_domain},
        "instruction": instruction(case.input_name, case.input_domain),
        "output_schema": OUTPUT_SCHEMA,
    }


def extract_response_text(body: dict[str, Any]) -> str | None:
    """Read an OpenAI-Responses-shaped text response without provider guessing."""
    for item in body.get("output", []):
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []) or []:
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                return content["text"]
    return None


def parse_structured_value(value: Any) -> dict[str, Any]:
    """Validate the normalized schema object after a provider-specific decode."""
    if not isinstance(value, dict):
        raise ValueError("response JSON was not an object")
    expected = set(OUTPUT_SCHEMA["properties"])
    if set(value) != expected:
        raise ValueError(f"response schema keys differ: got={sorted(value)}, expected={sorted(expected)}")
    if not isinstance(value.get("industries"), list) or not isinstance(value.get("sources"), list):
        raise ValueError("response list fields were not arrays")
    return value


def parse_response(body: dict[str, Any]) -> dict[str, Any]:
    """Decode the JSON-text envelope returned by Parallel Responses."""
    text = extract_response_text(body)
    if not text:
        raise ValueError("response had no output text")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("response text was not valid JSON") from exc
    return parse_structured_value(value)


def parse_exa_response(body: dict[str, Any]) -> dict[str, Any]:
    """Decode Exa deep-reasoning's object envelope (as used by funding)."""
    output = body.get("output")
    content = output.get("content") if isinstance(output, dict) else None
    if not isinstance(content, dict):
        raise ValueError("Exa response had no structured output content")
    citations: list[str] = []
    for field in output.get("grounding", []) or []:
        if not isinstance(field, dict):
            continue
        for citation in field.get("citations", []) or []:
            if isinstance(citation, dict) and isinstance(citation.get("url"), str):
                citations.append(citation["url"])
    return parse_structured_value({**content, "sources": list(dict.fromkeys(citations))})


def parse_exa_v2_response(body: dict[str, Any], *, input_domain: str) -> dict[str, Any]:
    """Recover supported scalar facts from Exa's retrieved organization entity.

    Exa currently returns citations for scalar fields while serializing those
    same fields as null in `output.content`. This only fills a null output from
    Exa's own structured search result; it never adds a second model or uses
    benchmark reference data.
    """
    value = parse_exa_response(body)
    results = [row for row in body.get("results", []) if isinstance(row, dict)]
    entities = [
        entity for row in results for entity in row.get("entities", []) or []
        if isinstance(entity, dict) and entity.get("type") == "company"
    ]
    properties = next(
        (entity.get("properties") for entity in entities if isinstance(entity.get("properties"), dict)),
        {},
    )
    if not isinstance(properties, dict):
        properties = {}

    def set_if_missing(field: str, candidate: Any) -> None:
        if value.get(field) in (None, "", []) and candidate not in (None, "", []):
            value[field] = candidate

    set_if_missing("legal_name", properties.get("name"))
    set_if_missing("founded_year", properties.get("foundedYear"))
    headquarters = properties.get("headquarters")
    if isinstance(headquarters, dict):
        set_if_missing("hq_city", headquarters.get("city"))
        set_if_missing("hq_country", headquarters.get("country"))
    workforce = properties.get("workforce")
    if isinstance(workforce, dict) and isinstance(workforce.get("total"), int):
        set_if_missing("headcount_min", workforce["total"])
        set_if_missing("headcount_max", workforce["total"])

    target = input_domain.lower().removeprefix("www.")
    # Only a LinkedIn result attached to the entity retrieved from the input
    # website can represent this company. Search results often include other
    # companies' LinkedIn pages as topical neighbors.
    target_entity_ids = {
        entity.get("id")
        for row in results
        if isinstance(row.get("url"), str)
        and (urlparse(row["url"]).hostname or "").lower().removeprefix("www.") == target
        for entity in row.get("entities", []) or []
        if isinstance(entity, dict) and entity.get("type") == "company" and entity.get("id")
    }
    for row in results:
        url = row.get("url")
        if not isinstance(url, str):
            continue
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower().removeprefix("www.")
        if hostname == target:
            set_if_missing("primary_domain", target)
        linked_entity_ids = {
            entity.get("id") for entity in row.get("entities", []) or []
            if isinstance(entity, dict) and entity.get("id")
        }
        if hostname == "linkedin.com" and parsed.path.lower().startswith("/company/") and target_entity_ids & linked_entity_ids:
            set_if_missing("linkedin_url", f"https://www.linkedin.com{parsed.path.rstrip('/')}")
    return parse_structured_value(value)


def normalized_company(value: dict[str, Any]) -> NormalizedCompany:
    return NormalizedCompany(
        legal_name=value.get("legal_name"),
        primary_domain=value.get("primary_domain"),
        hq_country=value.get("hq_country"),
        hq_city=value.get("hq_city"),
        founded_year=value.get("founded_year"),
        industry=value.get("industry"),
        industries=[str(item) for item in value.get("industries", []) if item],
        linkedin_url=value.get("linkedin_url"),
        headcount_min=value.get("headcount_min"),
        headcount_max=value.get("headcount_max"),
    )


def has_scored_value(value: dict[str, Any]) -> bool:
    return any(value.get(field) not in (None, "", []) for field in SCORING_FIELDS)
