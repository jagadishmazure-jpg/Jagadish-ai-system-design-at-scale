"""Strict tool contracts.

Every tool declares: name, description, Pydantic input and output schemas (extra keys rejected),
a permission scope, a risk tier (read_only / low_risk_write / high_risk_write), a timeout and a
retry policy. Errors come back in one structured shape. There is no free-text "do anything" tool;
the registry refuses to register one. High-risk tools cannot be called directly: only the
approval gate (approvals.py) can execute them, after deterministic checks and user confirmation.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .pii import redact_obj


class Tier(str, Enum):
    READ_ONLY = "read_only"
    LOW_RISK_WRITE = "low_risk_write"
    HIGH_RISK_WRITE = "high_risk_write"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ToolError(Strict):
    code: Literal["invalid_args", "forbidden", "not_found", "timeout", "upstream", "policy", "not_allowed"]
    message: str
    retryable: bool = False


# ---------------- fictional order data (Quillmoor Market) ----------------
DAY = 86400.0
NOW = 1_790_000_000.0  # fixed "today" so policy windows are deterministic
ORDERS = {
    "QM-10001": {"owner": "u-ana", "status": "delivered", "total": 59.99, "delivered_at": NOW - 10 * DAY,
                 "final_sale": False, "item": "trail backpack"},
    "QM-10002": {"owner": "u-ana", "status": "in_transit", "total": 24.50, "delivered_at": None,
                 "final_sale": False, "item": "water bottle", "eta_days": 2},
    "QM-10003": {"owner": "u-ben", "status": "delivered", "total": 140.00, "delivered_at": NOW - 45 * DAY,
                 "final_sale": False, "item": "rain jacket"},
    "QM-10004": {"owner": "u-ben", "status": "delivered", "total": 15.00, "delivered_at": NOW - 3 * DAY,
                 "final_sale": True, "item": "clearance socks"},
    "QM-10005": {"owner": "u-cy", "status": "processing", "total": 310.00, "delivered_at": None,
                 "final_sale": False, "item": "camp stove", "eta_days": 6},
}
ORDER_PATTERN = r"^QM-\d{5}$"


class OrderIn(Strict):
    order_id: str = Field(pattern=ORDER_PATTERN)


class OrderStatusOut(Strict):
    order_id: str
    status: str
    eta_days: int | None = None


class PolicyIn(Strict):
    query: str = Field(min_length=2, max_length=300)


class PolicyOut(Strict):
    chunks: list[dict]


class RefundIn(Strict):
    order_id: str = Field(pattern=ORDER_PATTERN)
    amount: float = Field(gt=0, le=10_000)
    reason: Literal["damaged", "not_as_described", "changed_mind", "other"]


class RefundOut(Strict):
    refund_id: str
    order_id: str
    amount: float


class CancelIn(Strict):
    order_id: str = Field(pattern=ORDER_PATTERN)


class HandoffOut(Strict):
    ticket_id: str
    queue: str


class EscalateIn(Strict):
    topic: Literal["cancellation", "billing_dispute", "refund_review", "other"]
    summary: str = Field(max_length=500)


@dataclass
class Tool:
    name: str
    description: str
    input_model: type[Strict]
    output_model: type[Strict]
    scope: str
    tier: Tier
    fn: object
    timeout_s: float = 0.5
    max_attempts: int = 2   # only retried when the error is retryable AND the tool is read-only


@dataclass
class ToolResult:
    ok: bool
    output: dict | None = None
    error: dict | None = None
    attempts: int = 0
    latency_s: float = 0.0
    redacted_args: dict = field(default_factory=dict)


class Registry:
    FORBIDDEN_NAMES = {"run", "execute", "do_anything", "shell", "sql", "eval", "http"}

    def __init__(self) -> None:
        self.tools: dict[str, Tool] = {}
        self.fail_next: dict[str, int] = {}   # chaos hook for tests: tool -> remaining failures

    def register(self, tool: Tool) -> None:
        if tool.name in self.FORBIDDEN_NAMES or not tool.description or tool.input_model is None:
            raise ValueError(f"refusing open-ended tool {tool.name!r}")
        if "query" in tool.input_model.model_fields and tool.tier != Tier.READ_ONLY:
            raise ValueError("write tools must take typed fields, not free text")
        self.tools[tool.name] = tool

    async def call(self, name: str, args: dict, identity: dict, *, _approved: bool = False) -> ToolResult:
        tool = self.tools.get(name)
        if tool is None:
            return ToolResult(False, error=ToolError(code="not_found", message="unknown tool").model_dump())
        red = redact_obj(args)
        if tool.tier == Tier.HIGH_RISK_WRITE and not _approved:
            return ToolResult(False, error=ToolError(code="not_allowed",
                              message="high-risk tool needs the approval gate").model_dump(), redacted_args=red)
        if tool.scope not in identity.get("scope", "").split():
            return ToolResult(False, error=ToolError(code="forbidden", message="missing scope").model_dump(),
                              redacted_args=red)
        try:
            parsed = tool.input_model.model_validate(args)
        except ValidationError as e:
            return ToolResult(False, error=ToolError(code="invalid_args",
                              message=f"{len(e.errors())} invalid field(s)").model_dump(), redacted_args=red)
        attempts, t0 = 0, time.perf_counter()
        limit = tool.max_attempts if tool.tier == Tier.READ_ONLY else 1
        last = None
        while attempts < limit:
            attempts += 1
            try:
                if self.fail_next.get(name, 0) > 0:
                    self.fail_next[name] -= 1
                    raise ConnectionError("injected upstream failure")
                out = await asyncio.wait_for(tool.fn(parsed, identity), tool.timeout_s)
                if isinstance(out, ToolError):
                    return ToolResult(False, error=out.model_dump(), attempts=attempts,
                                      latency_s=time.perf_counter() - t0, redacted_args=red)
                checked = tool.output_model.model_validate(out)
                return ToolResult(True, output=checked.model_dump(), attempts=attempts,
                                  latency_s=time.perf_counter() - t0, redacted_args=red)
            except TimeoutError:
                last = ToolError(code="timeout", message="tool timed out", retryable=True)
            except ConnectionError:
                last = ToolError(code="upstream", message="upstream unavailable", retryable=True)
            except ValidationError:
                last = ToolError(code="upstream", message="tool returned an invalid shape")
                break
        return ToolResult(False, error=last.model_dump(), attempts=attempts,
                          latency_s=time.perf_counter() - t0, redacted_args=red)


def build_registry(kb, store) -> Registry:
    reg = Registry()

    async def order_status(a: OrderIn, ident: dict):
        o = ORDERS.get(a.order_id)
        if o is None or o["owner"] != ident["sub"]:
            # same answer for "missing" and "someone else's": no order-id enumeration
            return ToolError(code="not_found", message="no such order on your account")
        return {"order_id": a.order_id, "status": o["status"], "eta_days": o.get("eta_days")}

    async def search_policy(a: PolicyIn, ident: dict):
        return {"chunks": kb.search(a.query)}

    async def issue_refund(a: RefundIn, ident: dict):
        key = f"refund:{a.order_id}"
        rid = "RF-" + a.order_id[3:]
        if not store.record_effect_once(key, {"refund_id": rid, "amount": a.amount, "by": ident["sub"]}):
            return ToolError(code="policy", message="order already refunded")
        return {"refund_id": rid, "order_id": a.order_id, "amount": a.amount}

    async def request_cancellation(a: CancelIn, ident: dict):
        # The AI never cancels: a confirmed request becomes a human-review ticket.
        store.record_effect_once(f"cancel-ticket:{a.order_id}", {"by": ident["sub"]})
        return {"ticket_id": "TK-C" + a.order_id[3:], "queue": "cancellations"}

    async def escalate(a: EscalateIn, ident: dict):
        tid = "TK-" + uuid.uuid4().hex[:8]
        store.record_effect_once(f"handoff:{tid}", {"topic": a.topic, "by": ident["sub"]})
        return {"ticket_id": tid, "queue": a.topic}

    reg.register(Tool("get_order_status", "Look up status and ETA for one of the caller's orders.",
                      OrderIn, OrderStatusOut, "orders:read", Tier.READ_ONLY, order_status))
    reg.register(Tool("search_policy", "Retrieve store policy passages relevant to a question.",
                      PolicyIn, PolicyOut, "chat", Tier.READ_ONLY, search_policy))
    reg.register(Tool("escalate_to_human", "Open a ticket for a human agent with a short summary.",
                      EscalateIn, HandoffOut, "chat", Tier.LOW_RISK_WRITE, escalate))
    reg.register(Tool("issue_refund", "Refund an eligible order to the original payment method.",
                      RefundIn, RefundOut, "chat", Tier.HIGH_RISK_WRITE, issue_refund, max_attempts=1))
    reg.register(Tool("request_cancellation", "File a confirmed cancellation request for human review.",
                      CancelIn, HandoffOut, "chat", Tier.HIGH_RISK_WRITE, request_cancellation, max_attempts=1))
    return reg
