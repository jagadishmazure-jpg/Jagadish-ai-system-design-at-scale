"""Intent router and the minimum-sufficient-intelligence ladder.

Tier 0  rules       : greetings, order status with an order id      -> no model call
Tier 1  classifier  : keyword-scored intents (cheap, local)          -> no model call to decide
Tier 2  small model : policy/FAQ answers grounded on retrieved chunks
Tier 3  large model : multi-part or complex requests only
HITL intents (cancellation, billing dispute) are routed to a human and never answered by AI.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

HITL_INTENTS = {"cancellation", "billing_dispute"}
ORDER_ID = re.compile(r"\bQM-\d{5}\b")
GREETING = re.compile(r"^\s*(hi|hello|hey|good (morning|afternoon|evening)|thanks|thank you)\b[\s!.,]*$", re.IGNORECASE)

KEYWORDS = {
    "order_status": ["where", "track", "status", "shipped", "arrive", "delivery", "package"],
    "refund_request": ["refund", "money back", "return my", "reimburse"],
    "cancellation": ["cancel", "cancellation", "close my account", "stop my subscription"],
    "billing_dispute": ["charged twice", "dispute", "chargeback", "unauthorized charge", "overcharged",
                        "double charged", "wrong amount"],
    "faq_policy": ["policy", "return", "shipping", "warranty", "how long", "international", "refund timing",
                   "price match", "duties", "password", "address", "damaged"],
}
OUT_OF_SCOPE = ["weather", "stock price", "write a poem", "homework", "election", "recipe"]


@dataclass
class Route:
    intent: str
    tier: str          # rules | classifier | small | large | human | none
    confidence: float
    order_id: str | None = None


def classify(text: str) -> tuple[str, float]:
    t = text.lower()
    scores = {k: sum(1 for kw in kws if kw in t) for k, kws in KEYWORDS.items()}
    if ORDER_ID.search(text) and scores["refund_request"]:
        scores["refund_request"] += 1   # a refund ask about a specific order is an action, not an FAQ
    # HITL intents win ties: when in doubt, a human handles money and cancellations.
    best = max(scores, key=lambda k: (scores[k], k in HITL_INTENTS, k == "faq_policy"))
    total = sum(scores.values())
    if scores[best] == 0:
        return "unknown", 0.0
    return best, scores[best] / total


def is_complex(text: str) -> bool:
    return len(text.split()) > 40 or text.count("?") >= 2 or bool(re.search(r"\b(compare|and also|explain why)\b",
                                                                              text, re.IGNORECASE))


def route(text: str, enabled: bool = True) -> Route:
    """enabled=False sends everything to the large model (the 'router off' baseline)."""
    oid = (m.group(0) if (m := ORDER_ID.search(text)) else None)
    if not enabled:
        intent, conf = classify(text)
        if intent in HITL_INTENTS:
            return Route(intent, "human", conf, oid)
        return Route(intent if intent != "unknown" else "complex", "large", conf, oid)
    if GREETING.match(text):
        return Route("greeting", "rules", 1.0)
    if any(w in text.lower() for w in OUT_OF_SCOPE):
        return Route("out_of_scope", "rules", 1.0)
    intent, conf = classify(text)
    if intent in HITL_INTENTS:
        return Route(intent, "human", conf, oid)
    if intent == "order_status" and oid:
        return Route("order_status", "rules", 1.0, oid)
    if intent == "refund_request":
        return Route("refund_request", "classifier", conf, oid)
    if intent == "order_status":
        return Route("order_status", "classifier", conf, oid)  # ask for the order id, no model needed
    if intent == "faq_policy" and not is_complex(text):
        return Route("faq_policy", "small", conf)
    if intent == "unknown" and not is_complex(text):
        return Route("unknown", "small", 0.0)
    return Route("complex" if intent == "unknown" else intent, "large", conf, oid)
