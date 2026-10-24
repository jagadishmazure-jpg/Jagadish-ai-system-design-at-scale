"""Commerce tools over fictional data. Every tool checks the caller owns the order (no BOLA)."""
from __future__ import annotations

import copy

from .guardrails import PolicyViolation, authorize_credit, authorize_refund

SEED_ORDERS = {
    "KG-100001": {"user_id": "u-ava", "status": "shipped", "eta": "2026-10-27", "total": 129.00, "carrier": "parcel"},
    "KG-100002": {"user_id": "u-ava", "status": "delivered", "eta": "2026-10-12", "total": 58.50, "carrier": "parcel"},
    "KG-100003": {"user_id": "u-ben", "status": "delivered", "eta": "2026-10-15", "total": 412.99, "carrier": "freight"},
    "KG-100004": {"user_id": "u-ben", "status": "processing", "eta": "2026-10-30", "total": 24.00, "carrier": "parcel"},
}
SEED_PRICES = {"SKU-HEADPHONES": 89.00, "SKU-KETTLE": 34.50}


class NotFound(Exception):
    pass


class Commerce:
    def __init__(self) -> None:
        self.orders = copy.deepcopy(SEED_ORDERS)
        self.prices = dict(SEED_PRICES)
        self.refunds: dict[str, dict] = {}  # idempotency key -> refund record
        self.credits: dict[str, int] = {}
        self.calls: list[str] = []

    def _owned(self, order_id: str, user_id: str) -> dict:
        o = self.orders.get(order_id)
        if o is None or o["user_id"] != user_id:
            raise NotFound("order not found")  # same error for missing and not-yours: no enumeration
        return o

    def order_status(self, order_id: str, user_id: str) -> dict:
        self.calls.append("order_status")
        o = self._owned(order_id, user_id)
        return {"order_id": order_id, "status": o["status"], "eta": o["eta"]}

    def price(self, sku: str) -> float:
        self.calls.append("price")
        return self.prices[sku]

    def refunded_total(self, order_id: str) -> float:
        return sum(r["amount"] for r in self.refunds.values() if r["order_id"] == order_id)

    def issue_refund(self, order_id: str, user_id: str, amount: float, idempotency_key: str) -> dict:
        self.calls.append("issue_refund")
        if idempotency_key in self.refunds:
            return self.refunds[idempotency_key]
        o = self._owned(order_id, user_id)
        authorize_refund(o, user_id, amount, self.refunded_total(order_id))
        rec = {"order_id": order_id, "amount": round(amount, 2), "refund_id": f"RF-{len(self.refunds) + 1:05d}"}
        self.refunds[idempotency_key] = rec
        return rec

    def goodwill_credit(self, order_id: str, user_id: str, pct: float) -> dict:
        self.calls.append("goodwill_credit")
        o = self._owned(order_id, user_id)
        authorize_credit(o, user_id, pct, self.credits.get(order_id, 0))
        self.credits[order_id] = self.credits.get(order_id, 0) + 1
        return {"order_id": order_id, "credit": round(o["total"] * pct / 100, 2)}


__all__ = ["Commerce", "NotFound", "PolicyViolation"]
