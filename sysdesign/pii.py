"""PII redaction applied to input, output, logs, traces and cache keys.

Cards are only redacted when the digit run passes a Luhn check, so order numbers
are not destroyed. Emails, phones and SSNs are pattern-based. Redaction runs
before anything is stored anywhere (cache, trace, log, checkpoint).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
PHONE = re.compile(r"(?<![\w-])(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}(?![\w-])")
CARD_RUN = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")


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
    if 13 <= len(digits) <= 19 and luhn_ok(digits):
        return "[CARD]"
    return m.group(0)


def redact(text: str) -> str:
    if not text:
        return text
    text = EMAIL.sub("[EMAIL]", text)
    text = CARD_RUN.sub(_card, text)
    text = SSN.sub("[SSN]", text)
    text = PHONE.sub("[PHONE]", text)
    return text


def redact_obj(obj):
    """Recursively redact every string in a dict/list structure."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v) for v in obj]
    return obj


def contains_pii(text: str) -> bool:
    return redact(text) != text


def pseudonymise(value: str) -> str:
    """Keyed HMAC pseudonym for user ids in metrics/traces. Key from PII_HMAC_KEY (rotate by changing it)."""
    key = os.environ.get("PII_HMAC_KEY", "dev-only-key-rotate-me").encode()
    return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()[:16]
