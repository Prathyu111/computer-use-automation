"""Redact secrets and high-sensitivity values from logs and artifacts."""

from __future__ import annotations

import re

from cua.models import Sensitivity

_SECRET_KEYS = re.compile(
    r"(password|token|secret|authorization|cookie|ssn|account[_-]?number)",
    re.I,
)
_LONG_DIGIT = re.compile(r"\b\d{9,}\b")


def redact_text(text: str) -> str:
    if not text:
        return text
    out = _LONG_DIGIT.sub("[REDACTED]", text)
    return out


def redact_value(name: str, value: str, sensitivity: Sensitivity | None = None) -> str:
    if sensitivity in {Sensitivity.secret, Sensitivity.pii}:
        return "[REDACTED]"
    if sensitivity is Sensitivity.identifier:
        if len(value) <= 2:
            return "[id]"
        return value[0] + "…" + value[-1]
    if _SECRET_KEYS.search(name):
        return "[REDACTED]"
    return redact_text(value)


def looks_like_secret_field(name: str) -> bool:
    return bool(_SECRET_KEYS.search(name or ""))
