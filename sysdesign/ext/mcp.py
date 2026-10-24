"""MCP-style domain tool servers.

Tools are grouped into separate servers by domain (orders, payments, account) so each domain owns
its contracts and can be deployed and versioned independently. Each server answers
`tools/list` (name, description, inputSchema, tier) and `tools/call` in a JSON-RPC-like shape.
The single agent consumes them through one client that merges the catalogues.

Rule used in this repo: a CAPABILITY is a tool; another AUTONOMOUS DECISION MAKER is an agent.
Calculators, lookups and refunds are capabilities, so they are tools, not agents. The only
agent-to-agent handoff is to the human-staffed queue, which makes its own decisions.
"""
from __future__ import annotations

from pydantic import Field

from .calc import refund_amount, tax
from .toolkit import Registry, Strict, Tier, Tool

DOMAINS = {
    "orders": {"get_order_status"},
    "payments": {"issue_refund", "calc_refund_amount", "calc_tax"},
    "account": {"escalate_to_human", "request_cancellation", "search_policy"},
}


class RefundCalcIn(Strict):
    item_price: float = Field(ge=0, le=10_000)
    shipping: float = Field(ge=0, le=1_000)
    reason: str = Field(pattern=r"^(damaged|not_as_described|changed_mind|other)$")


class TaxIn(Strict):
    amount: float = Field(ge=0, le=100_000)
    region: str = Field(default="default", pattern=r"^[a-z_]{2,20}$")


class AmountOut(Strict):
    amount: float


def add_calculators(reg: Registry) -> None:
    async def calc_refund(a: RefundCalcIn, ident):
        return {"amount": refund_amount(a.item_price, a.shipping, a.reason)}

    async def calc_tax(a: TaxIn, ident):
        return {"amount": tax(a.amount, a.region)}

    reg.register(Tool("calc_refund_amount", "Compute a refund amount from price, shipping and reason.",
                      RefundCalcIn, AmountOut, "chat", Tier.READ_ONLY, calc_refund))
    reg.register(Tool("calc_tax", "Compute sales tax for an amount.", TaxIn, AmountOut, "chat",
                      Tier.READ_ONLY, calc_tax))


class DomainServer:
    def __init__(self, domain: str, registry: Registry) -> None:
        self.domain = domain
        self.registry = registry
        self.names = DOMAINS[domain]

    def handle(self, request: dict):
        """Synchronous part of the protocol (tools/list). tools/call is async: see call()."""
        if request.get("method") == "tools/list":
            return {"tools": [{"name": n, "description": self.registry.tools[n].description,
                               "inputSchema": self.registry.tools[n].input_model.model_json_schema(),
                               "tier": self.registry.tools[n].tier.value}
                              for n in sorted(self.names) if n in self.registry.tools]}
        return {"error": {"code": -32601, "message": "method not found"}}

    async def call(self, name: str, args: dict, identity: dict):
        if name not in self.names:
            return {"error": {"code": -32602, "message": f"{name} is not served by {self.domain}"}}
        res = await self.registry.call(name, args, identity)
        return {"ok": res.ok, "output": res.output, "error": res.error}


class ToolClient:
    """What the agent sees: a merged catalogue; each call goes to the owning domain server."""

    def __init__(self, servers: list[DomainServer]) -> None:
        self.servers = {s.domain: s for s in servers}
        self.owner = {n: s.domain for s in servers for n in s.names}

    def catalogue(self) -> list[dict]:
        return [t for s in self.servers.values() for t in s.handle({"method": "tools/list"})["tools"]]

    async def call(self, name: str, args: dict, identity: dict):
        dom = self.owner.get(name)
        if dom is None:
            return {"ok": False, "error": {"code": "not_found"}}
        return await self.servers[dom].call(name, args, identity)
