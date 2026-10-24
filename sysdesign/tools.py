"""Commerce tools over fictional data. Every tool checks the caller owns the order (no BOLA)."""
from __future__ import annotations

import copy
import json
from collections.abc import MutableMapping

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


class RedisJSONMap(MutableMapping):
    """A dict-shaped view of one Redis hash with JSON values, so every replica sees the same data.

    Values are copies: change a nested value by assigning the whole value back (no code here
    mutates nested values in place).
    """

    def __init__(self, redis, key: str) -> None:
        self.r, self.key = redis, key

    def __getitem__(self, k):
        v = self.r.hget(self.key, k)
        if v is None:
            raise KeyError(k)
        return json.loads(v)

    def __setitem__(self, k, v) -> None:
        self.r.hset(self.key, k, json.dumps(v))

    def __delitem__(self, k) -> None:
        if not self.r.hdel(self.key, k):
            raise KeyError(k)

    def __iter__(self):
        return iter(sorted(x.decode() if isinstance(x, bytes) else x for x in self.r.hkeys(self.key)))

    def __len__(self) -> int:
        return self.r.hlen(self.key)

    def __eq__(self, other) -> bool:
        return dict(self.items()) == dict(other)

    def seed(self, values: dict) -> None:
        for k, v in values.items():
            self.r.hsetnx(self.key, k, json.dumps(v))


class Commerce:
    """Orders, refunds, credits and carts. With `redis`, all of it lives in Redis (shared by every
    replica and worker); without it, plain dicts (one process, tests)."""

    def __init__(self, redis=None, prefix: str = "shop") -> None:
        self.redis = redis
        if redis is None:
            self.orders = copy.deepcopy(SEED_ORDERS)
            self.prices = dict(SEED_PRICES)
            self.refunds: MutableMapping = {}  # idempotency key -> refund record
            self.credits: MutableMapping = {}
            self.carts: MutableMapping = {}    # user id -> {sku: qty}
        else:
            self.orders = RedisJSONMap(redis, f"{prefix}:orders")
            self.prices = RedisJSONMap(redis, f"{prefix}:prices")
            self.refunds = RedisJSONMap(redis, f"{prefix}:refunds")
            self.credits = RedisJSONMap(redis, f"{prefix}:credits")
            self.carts = RedisJSONMap(redis, f"{prefix}:carts")
            self.orders.seed(SEED_ORDERS)
            self.prices.seed(SEED_PRICES)
        self._prefix = prefix
        self.calls: list[str] = []  # per-process call trace for tests, not business state

    def _next_refund_no(self) -> int:
        if self.redis is not None:
            return int(self.redis.incr(f"{self._prefix}:refund_seq"))
        return len(self.refunds) + 1

    def add_to_cart(self, user_id: str, sku: str, qty: int = 1) -> dict:
        if sku not in self.prices:
            raise NotFound("unknown sku")
        if not isinstance(qty, int) or qty < 1:
            raise ValueError("qty must be a positive whole number")
        cart = dict(self.carts.get(user_id, {}))
        cart[sku] = cart.get(sku, 0) + qty
        self.carts[user_id] = cart
        return cart

    def cart(self, user_id: str) -> dict:
        return dict(self.carts.get(user_id, {}))

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
        rec = {"order_id": order_id, "amount": round(amount, 2), "refund_id": f"RF-{self._next_refund_no():05d}"}
        self.refunds[idempotency_key] = rec
        return rec

    def goodwill_credit(self, order_id: str, user_id: str, pct: float) -> dict:
        self.calls.append("goodwill_credit")
        o = self._owned(order_id, user_id)
        authorize_credit(o, user_id, pct, self.credits.get(order_id, 0))
        self.credits[order_id] = self.credits.get(order_id, 0) + 1
        return {"order_id": order_id, "credit": round(o["total"] * pct / 100, 2)}


__all__ = ["Commerce", "NotFound", "PolicyViolation"]
