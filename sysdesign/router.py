"""Intent router implementing a minimum-sufficient-intelligence ladder.

Rung 0  rules        greetings, thanks, order-status lookups, HITL triggers
Rung 1  classifier   small keyword-weight classifier (stand-in for a trained model)
Rung 2  small model  policy questions answered with retrieval (RAG)
Rung 3  large model  only for requests the classifier marks complex
HITL                 cancellations, billing disputes, legal threats: never AI-handled

HITL is checked first and wins over every other signal, so "thanks, also I
want to dispute this charge" goes to a human, not to the greeting rule.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .guardrails import normalize

HITL_PATTERNS = [
    r"\bcancel\w* (my |the )?(membership|subscription|account|plan)\b",
    r"\b(billing|charge) dispute\b", r"\bdispute (a |the |this |my )?(charge|bill|payment)\b",
    r"\bchargeback\b", r"\b(lawyer|attorney|sue|lawsuit)\b", r"\bfraud\w*\b",
    r"\bspeak (to|with) a (human|person|agent)\b",
]
_HITL = [re.compile(p) for p in HITL_PATTERNS]
_GREETING = re.compile(r"^(hi|hello|hey|good (morning|afternoon|evening))[!. ]*$")
_THANKS = re.compile(r"^(thanks|thank you|thx|cheers)[!. ]*( so much)?[!. ]*$")
_ORDER_ID = re.compile(r"\b(KG-\d{6})\b", re.I)
_STATUS = re.compile(r"\b(where|status|track|tracking|arrive|shipped)\b")

_CLASSES = {
    "refund": {"refund": 2.0, "money back": 2.0, "return": 1.0, "reimburse": 1.5},
    "price": {"price": 2.0, "cost": 1.2, "how much": 1.5, "cheaper": 1.0},
    "policy": {"policy": 1.5, "return": 1.0, "window": 1.0, "shipping": 1.2, "cash": 1.0, "delivery": 0.6,
               "gift card": 1.2, "days": 0.5, "exchange": 0.8, "express": 1.0, "pay": 0.8},
}
_COMPLEX = {"split shipment": 2.0, "partial": 1.0, "multiple orders": 1.5, "two orders": 1.5, "and also": 0.6,
            "damaged": 0.8, "missing item": 1.2, "compare": 0.6}

LADDER = ("rules", "classifier", "small_model", "large_model", "human")


@dataclass(frozen=True)
class Route:
    intent: str
    rung: str
    model_tier: str | None  # None = no LLM call
    confidence: float
    order_id: str | None = None


def classify(text: str) -> tuple[str, float]:
    t = normalize(text)
    scores = {c: sum(w for kw, w in kws.items() if kw in t) for c, kws in _CLASSES.items()}
    best = max(scores, key=scores.get)
    total = sum(scores.values())
    if scores[best] == 0:
        return "unknown", 0.0
    return best, scores[best] / total


def complexity(text: str) -> float:
    t = normalize(text)
    return sum(w for kw, w in _COMPLEX.items() if kw in t) + (1.0 if len(t) > 400 else 0.0)


def route(text: str, ladder_enabled: bool = True) -> Route:
    t = normalize(text)
    if any(p.search(t) for p in _HITL):
        return Route("escalate", "human", None, 1.0)
    m = _ORDER_ID.search(text)
    order_id = m.group(1).upper() if m else None
    if not ladder_enabled:
        intent, conf = classify(text)
        if order_id and _STATUS.search(t):
            intent = "order_status"
        return Route(intent, "large_model", "large", conf, order_id)
    if _GREETING.match(t):
        return Route("greeting", "rules", None, 1.0)
    if _THANKS.match(t):
        return Route("thanks", "rules", None, 1.0)
    if order_id and _STATUS.search(t) and "refund" not in t:
        return Route("order_status", "rules", None, 1.0, order_id)
    intent, conf = classify(text)
    if complexity(text) >= 1.5:
        return Route(intent if intent != "unknown" else "complex", "large_model", "large", conf, order_id)
    if intent == "refund":
        return Route("refund", "classifier", "small", conf, order_id)
    if intent == "price":
        return Route("price", "classifier", None, conf, order_id)  # live pricing API, never the model
    if intent == "policy":
        return Route("policy", "small_model", "small", conf, order_id)
    return Route("unknown", "small_model", "small", conf, order_id)
