from __future__ import annotations

import re
import time
from typing import Any

import requests


class ProviderHTTPError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        elapsed_ms: int | None = None,
        response_headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.elapsed_ms = elapsed_ms
        self.response_headers = response_headers or {}
        self.retry_after = self.response_headers.get("retry-after")
        super().__init__(f"HTTP {status_code}: {message}")


def request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    json_body: Any = None,
    timeout: int = 45,
) -> tuple[Any, int]:
    body, elapsed_ms, _ = request_json_with_headers(
        method,
        url,
        headers=headers,
        params=params,
        json_body=json_body,
        timeout=timeout,
    )
    return body, elapsed_ms


def request_json_with_headers(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    json_body: Any = None,
    timeout: int = 45,
) -> tuple[Any, int, dict[str, str]]:
    started = time.perf_counter()
    response = requests.request(
        method,
        url,
        headers=headers,
        params=params,
        json=json_body,
        timeout=timeout,
    )
    elapsed_ms = round((time.perf_counter() - started) * 1000)
    try:
        body = response.json()
    except ValueError:
        body = None
    if not response.ok:
        message = ""
        if isinstance(body, dict):
            message = str(body.get("message") or body.get("error") or body.get("detail") or "")
        if not message:
            message = response.text[:300]
        safe_headers = {
            key.lower(): value
            for key, value in response.headers.items()
            if key.lower()
            in {
                "retry-after",
                "x-ratelimit-limit",
                "x-ratelimit-remaining",
                "x-ratelimit-reset",
                "ratelimit-limit",
                "ratelimit-remaining",
                "ratelimit-reset",
                "x-call-credits-spent",
            }
        }
        raise ProviderHTTPError(
            response.status_code,
            message,
            elapsed_ms=elapsed_ms,
            response_headers=safe_headers,
        )
    return body, elapsed_ms, dict(response.headers)


def nested(value: Any, *path: str, default: Any = None) -> Any:
    current = value
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def first_value(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", [], {}):
            if isinstance(value, dict) and "value" in value:
                value = value["value"]
            return value
    return None


def first_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return next((item for item in value if isinstance(item, dict)), {})
    return {}


def as_list(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    return value if isinstance(value, list) else [value]


def parse_employee_range(value: Any) -> tuple[int | None, int | None]:
    if value is None:
        return None, None
    if isinstance(value, dict):
        low = first_value(value, "min", "minimum", "low", "from", "gte", "employees_min")
        high = first_value(value, "max", "maximum", "high", "to", "lte", "employees_max")
        return _int(low), _int(high)
    if isinstance(value, (int, float)):
        number = int(value)
        return number, number
    text = str(value).replace(",", "")
    numbers = [int(v) for v in re.findall(r"\d+", text)]
    if not numbers:
        return None, None
    if len(numbers) == 1:
        if "+" in text:
            return numbers[0], None
        return numbers[0], numbers[0]
    return numbers[0], numbers[1]


def _int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return None


def find_first_key(value: Any, key: str) -> Any:
    """Depth-first lookup used only to bridge lightly nested vendor envelopes."""
    if isinstance(value, dict):
        if key in value and value[key] not in (None, "", [], {}):
            return value[key]
        for child in value.values():
            found = find_first_key(child, key)
            if found not in (None, "", [], {}):
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_first_key(child, key)
            if found not in (None, "", [], {}):
                return found
    return None
