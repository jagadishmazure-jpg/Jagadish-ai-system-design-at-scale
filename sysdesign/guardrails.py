"""Input and output guardrails plus an action policy for tool calls.

Input: rule patterns for known injection phrasings plus a small weighted
feature classifier (a stand-in for a trained guard model) so paraphrases
that dodge the exact rules still score high.
Output: never state discounts/credits beyond policy, never make binding
offers, never quote a live price from model text, always redact PII.
Actions: refund and credit tool calls are validated against the order and
policy before execution, whatever the model asked for.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .pii import redact

_RULES = [
    r"ignore (all |any |your |the )?(previous |prior |above )?(instructions|rules|prompt)",
    r"disregard (all |your |the )?(previous |prior )?(instructions|rules|guidelines)",
    r"you are now\b", r"system prompt", r"developer mode", r"jailbreak",
    r"pretend (you|to) (are|be)", r"(agree|say yes) (with|to) (anything|everything)",
    r"legally binding", r"no takesies",
]
_RULE_RE = [re.compile(p) for p in _RULES]
_FEATURES = {
    "ignore": 0.35, "instructions": 0.3, "override": 0.35, "forget": 0.25, "rules": 0.15, "objective": 0.2,
    "pretend": 0.3, "roleplay": 0.3, "binding": 0.3, "100%": 0.25, "free": 0.1, "prompt": 0.2,
    "policy": 0.05, "admin": 0.2, "unrestricted": 0.35, "bypass": 0.35,
}
INJECTION_THRESHOLD = 0.6


def normalize(text: str) -> str:
    """NFKC + strip zero-width chars + collapse whitespace, so obfuscation doesn't dodge rules."""
    t = unicodedata.normalize("NFKC", text)
    t = re.sub(r"[\u200b-\u200f\u2060\ufeff]", "", t)
    return re.sub(r"\s+", " ", t).strip().lower()


def injection_score(text: str) -> float:
    t = normalize(text)
    words = set(re.findall(r"[a-z0-9%]+", t))
    return min(1.0, sum(w for f, w in _FEATURES.items() if f in words))


@dataclass
class GuardResult:
    allowed: bool
    reason: str = "ok"
    text: str = ""


def check_input(text: str, max_chars: int = 4000) -> GuardResult:
    if len(text) > max_chars:
        return GuardResult(False, "too_long")
    t = normalize(text)
    if any(r.search(t) for r in _RULE_RE):
        return GuardResult(False, "injection_rule")
    if injection_score(text) >= INJECTION_THRESHOLD:
        return GuardResult(False, "injection_classifier")
    return GuardResult(True, "ok", redact(text))


_DISCOUNT = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*(%|percent)\s*(off|discount|credit)", re.I)
_BINDING = re.compile(r"(legally binding|that'?s a deal|we guarantee|i promise)", re.I)
_PRICE = re.compile(r"(?:\$\s?\d|\d+\s?dollars)", re.I)
MAX_CREDIT_PCT = 10.0


def check_output(text: str, intent: str, allowed_prices: bool = False) -> GuardResult:
    for m in _DISCOUNT.finditer(text):
        if float(m.group(1)) > MAX_CREDIT_PCT:
            return GuardResult(False, "discount_beyond_policy")
    if _BINDING.search(text):
        return GuardResult(False, "binding_offer")
    if intent == "price" and _PRICE.search(text) and not allowed_prices:
        return GuardResult(False, "price_from_model")
    return GuardResult(True, "ok", redact(text))


class PolicyViolation(Exception):
    pass


def authorize_refund(order: dict, user_id: str, amount: float, already_refunded: float) -> None:
    if order["user_id"] != user_id:
        raise PolicyViolation("order does not belong to caller")
    if not (amount > 0):  # also rejects NaN
        raise PolicyViolation("refund amount must be positive")
    if amount + already_refunded > order["total"] + 1e-9:
        raise PolicyViolation("refund exceeds order total")
    if order["status"] not in ("delivered", "returned"):
        raise PolicyViolation("order not eligible for refund yet")


def authorize_credit(order: dict, user_id: str, pct: float, credits_on_order: int) -> None:
    if order["user_id"] != user_id:
        raise PolicyViolation("order does not belong to caller")
    if not (0 < pct <= MAX_CREDIT_PCT):
        raise PolicyViolation(f"credit must be in (0, {MAX_CREDIT_PCT}] percent")
    if credits_on_order >= 1:
        raise PolicyViolation("one goodwill credit per order")
