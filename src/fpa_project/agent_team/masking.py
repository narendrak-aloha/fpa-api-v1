"""Fail-closed removal/masking of sensitive planning data before LLM calls."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import Any


SENSITIVE_KEYS = {
    "resource_employee", "national_id", "compensation", "customer",
    "customer_name", "employee", "employee_name", "salary", "pay_rate",
}


def _token(value: Any) -> str:
    # A deterministic token preserves grouping/debug usefulness without
    # allowing the original value into model context.
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]
    return f"<masked:{digest}>"


def mask_for_llm(value: Any) -> Any:
    """Return a deep copy safe for model context.

    Sensitive values are replaced by deterministic opaque tokens.  Unknown
    object types are rejected instead of being stringified into model input.
    """
    if isinstance(value, Mapping):
        # Recurse through nested request/context structures and fail closed
        # for non-string keys so masking cannot be bypassed accidentally.
        result: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str):
                raise TypeError("context keys must be strings")
            key = raw_key.lower()
            result[raw_key] = _token(raw_value) if key in SENSITIVE_KEYS else mask_for_llm(raw_value)
        return result
    if isinstance(value, list):
        return [mask_for_llm(item) for item in value]
    if isinstance(value, tuple):
        return [mask_for_llm(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool, Decimal, date, datetime)):
        return value
    raise TypeError(f"unsupported context value: {type(value).__name__}")


def mask_request_text(text: str) -> str:
    """Mask sensitive key/value fragments in free-form NL before model input."""
    if not isinstance(text, str):
        raise TypeError("request must be a string")
    sensitive = "|".join(re.escape(key) for key in sorted(SENSITIVE_KEYS, key=len, reverse=True))
    # Only key/value fragments are replaced; ordinary prose remains readable
    # for the planner while named sensitive fields are protected.
    pattern = re.compile(rf"(?i)\b(?:{sensitive})\b\s*(?:=|:|is)\s*([^,;\n]+)")
    return pattern.sub(lambda match: match.group(0)[:match.start(1) - match.start(0)] + _token(match.group(1).strip()), text)
