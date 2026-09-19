"""Redact secrets and high-sensitivity values from logs and artifacts."""

from __future__ import annotations

import re
from typing import Any

from cua.models import Sensitivity

_SECRET_KEYS = re.compile(
    r"(password|token|secret|authorization|cookie|ssn|account[_-]?number|api[_-]?key)",
    re.I,
)
_DIGIT_RUN = re.compile(r"\b\d{4,}\b")


def redact_text(text: str) -> str:
    if not text:
        return text
    return _DIGIT_RUN.sub("[REDACTED]", text)


def redact_value(name: str, value: str, sensitivity: Sensitivity | None = None) -> str:
    if sensitivity in {Sensitivity.secret, Sensitivity.pii}:
        return "[REDACTED]"
    if sensitivity is Sensitivity.identifier:
        if len(value) <= 2:
            return "[id]"
        return value[0] + "…" + value[-1]
    if _SECRET_KEYS.search(name or ""):
        return "[REDACTED]"
    if name in {"value", "memberId", "member_id"}:
        return redact_value(name, value, Sensitivity.identifier)
    return redact_text(value)


def looks_like_secret_field(name: str) -> bool:
    return bool(_SECRET_KEYS.search(name or ""))


def redact_for_log(key: str, value: Any) -> Any:
    if isinstance(value, dict):
        return {k: redact_for_log(k, v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_for_log(key, item) for item in value]
    if isinstance(value, str):
        return redact_value(key, value)
    return value
