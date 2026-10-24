"""PII redaction applied to input, output, logs, traces and cache entries.

Cards are only redacted when the digit run passes a Luhn check, so order
numbers are not mangled; emails, US phone numbers and SSNs use patterns.
"""
from __future__ import annotations

import re

_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_SSN = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
_PHONE = re.compile(r"(?<![\w])(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}(?!\d)")


def luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def _card(m: re.Match) -> str:
    digits = re.sub(r"\D", "", m.group(0))
    return "[CARD]" if 13 <= len(digits) <= 19 and luhn_ok(digits) else m.group(0)


def redact(text: str) -> str:
    if not text:
        return text
    text = _EMAIL.sub("[EMAIL]", text)
    text = _CARD.sub(_card, text)
    text = _SSN.sub("[SSN]", text)
    text = _PHONE.sub("[PHONE]", text)
    return text


def contains_pii(text: str) -> bool:
    return redact(text) != text


def redact_obj(obj):
    """Recursively redact strings inside dicts/lists (for logs and span attributes)."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v) for v in obj]
    return obj
