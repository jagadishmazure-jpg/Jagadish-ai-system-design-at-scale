"""Decision-specific context builder.

Raw customer data is large (long order history, many past tickets, long chats). Each decision
gets only the fields it needs:
  funnel(decision) -> whitelisted fields from the relevant records only,
  compact(conversation) -> running summary + last N turns instead of full history,
  offload(big tool result) -> stored blob + short reference in the prompt,
  retrieval is a tool (search_policy), not a pre-stuffed context dump.
Token counts before and after are measured with the same counter the fake providers bill with.
"""
from __future__ import annotations

import hashlib
import json
import random
import re

from .llm import count_tokens
from .pii import redact

DECISION_FIELDS = {
    "refund_eligibility": {"order": ["order_id", "status", "total", "days_since_delivery", "final_sale"],
                           "history": [], "tickets": ["open_refund_tickets"]},
    "order_status": {"order": ["order_id", "status", "eta_days"], "history": [], "tickets": []},
    "policy_answer": {"order": [], "history": [], "tickets": []},
    "risk_assessment": {"order": ["order_id", "total"], "history": ["refunds_last_90d", "orders_last_90d"],
                        "tickets": ["open_disputes"]},
}


def synthetic_customer(n_orders: int = 120, n_tickets: int = 40, seed: int = 5) -> dict:
    """Fictional heavy customer: lots of history, tickets with PII, long notes."""
    rng = random.Random(seed)
    orders = [{"order_id": f"QM-{20000 + i}", "status": rng.choice(["delivered", "returned", "in_transit"]),
               "total": round(rng.uniform(5, 300), 2), "days_since_delivery": rng.randint(1, 700),
               "final_sale": rng.random() < 0.1, "eta_days": None,
               "items": [f"item-{rng.randint(1, 999)}" for _ in range(rng.randint(1, 6))],
               "shipping_address": f"{rng.randint(1, 999)} Example Lane, Springfield",
               "notes": "customer asked about gift wrap and delivery window " * rng.randint(1, 4)}
              for i in range(n_orders)]
    tickets = [{"ticket_id": f"TK-{i:04d}", "topic": rng.choice(["refund", "shipping", "dispute", "other"]),
                "open": rng.random() < 0.1,
                "transcript": ("Customer wrote from ana.r@example.com about a late parcel and asked for an "
                               "update; agent apologised and shared tracking. ") * rng.randint(2, 6)}
               for i in range(n_tickets)]
    return {"user": "u-ana", "orders": orders, "tickets": tickets}


def raw_context(customer: dict) -> str:
    return json.dumps(customer)


def funnel(decision: str, customer: dict, order_id: str | None = None) -> dict:
    spec = DECISION_FIELDS[decision]
    out: dict = {}
    if spec["order"] and order_id:
        o = next((o for o in customer["orders"] if o["order_id"] == order_id), None)
        if o:
            out["order"] = {k: o.get(k) for k in spec["order"]}
    if "refunds_last_90d" in spec["history"]:
        recent = [o for o in customer["orders"] if o["days_since_delivery"] <= 90]
        out["history"] = {"orders_last_90d": len(recent),
                          "refunds_last_90d": sum(o["status"] == "returned" for o in recent)}
    if spec["tickets"]:
        t = customer["tickets"]
        out["tickets"] = {}
        if "open_refund_tickets" in spec["tickets"]:
            out["tickets"]["open_refund_tickets"] = sum(x["open"] and x["topic"] == "refund" for x in t)
        if "open_disputes" in spec["tickets"]:
            out["tickets"]["open_disputes"] = sum(x["open"] and x["topic"] == "dispute" for x in t)
    return out


def compact(turns: list[str], keep_last: int = 2, max_summary_words: int = 60) -> dict:
    """Deterministic summary: intents and order ids mentioned earlier, plus the last turns verbatim."""
    older, last = turns[:-keep_last] if keep_last else turns, turns[-keep_last:] if keep_last else []
    orders = sorted(set(re.findall(r"\bQM-\d{5}\b", " ".join(older))))
    topics = sorted({w for t in older for w in ("refund", "return", "shipping", "damaged", "cancel", "warranty")
                     if w in t.lower()})
    summary = f"Earlier: {len(older)} turns; topics {', '.join(topics) or 'none'}; orders {', '.join(orders) or 'none'}."
    summary = " ".join(summary.split()[:max_summary_words])
    return {"summary": redact(summary), "recent": [redact(t) for t in last]}


class BlobStore:
    """Large tool results are offloaded; the prompt gets a reference and a short preview."""

    def __init__(self) -> None:
        self.blobs: dict[str, str] = {}

    def offload(self, payload, preview_chars: int = 160, threshold_tokens: int = 200) -> str:
        text = payload if isinstance(payload, str) else json.dumps(payload)
        if count_tokens(text) <= threshold_tokens:
            return text
        ref = "blob:" + hashlib.sha256(text.encode()).hexdigest()[:12]
        self.blobs[ref] = text
        return f"[{ref}, {count_tokens(text)} tokens offloaded] {text[:preview_chars]}"

    def get(self, ref: str) -> str:
        return self.blobs[ref]


def measure(decision: str = "refund_eligibility", seed: int = 5) -> dict:
    c = synthetic_customer(seed=seed)
    oid = c["orders"][3]["order_id"]
    before = count_tokens(raw_context(c).replace(",", " , "))
    after = count_tokens(json.dumps(funnel(decision, c, oid)).replace(",", " , "))
    turns = [f"turn {i}: question about order {oid} refund and shipping details please" for i in range(30)]
    conv_before = count_tokens(" ".join(turns))
    conv_after = count_tokens(json.dumps(compact(turns)))
    return {"decision": decision, "raw_tokens": before, "funnel_tokens": after,
            "reduction_pct": round(100 * (1 - after / before), 1),
            "conversation_tokens": conv_before, "compacted_tokens": conv_after}
