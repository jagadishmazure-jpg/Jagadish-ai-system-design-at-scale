"""Authorization/policy layer, outside the agent.

The agent proposes an action. This layer alone decides: allow / deny / needs_approval, using
the verified token (sub, tenant, scope, tier claims) and order data. It never reads entitlement
from the chat text: a user typing "I'm premium" changes nothing. Approvals are recorded by this
layer; the agent has no API to mark its own proposal approved.
"""
from __future__ import annotations

from dataclasses import dataclass

from .approvals import policy_check
from .governance import AUTHORITY

ENTITLEMENTS = {
    "standard": {"refund_limit_usd": 100.00, "expedited_replacement": False},
    "premium": {"refund_limit_usd": 250.00, "expedited_replacement": True},
}


def tier_of(identity: dict) -> str:
    t = identity.get("tier", "standard")
    return t if t in ENTITLEMENTS else "standard"


@dataclass
class Verdict:
    decision: str          # allow | deny | needs_user_confirmation | needs_human_approval
    reason: str = ""


class PolicyEngine:
    def __init__(self, store=None) -> None:
        self.store = store

    def decide(self, action: str, args: dict, identity: dict) -> Verdict:
        ent = ENTITLEMENTS[tier_of(identity)]
        if action == "expedited_replacement":
            if not ent["expedited_replacement"]:
                return Verdict("deny", "not_entitled")
            return Verdict("needs_user_confirmation")
        if action == "grant_discount":
            return Verdict("needs_human_approval", "authority:" + AUTHORITY["grant_discount_or_credit"])
        if action in ("issue_refund", "request_cancellation"):
            chk = policy_check(action, args, identity, store=self.store)
            if not chk.ok:
                if chk.reason in ("outside_window", "final_sale"):
                    return Verdict("deny", chk.reason)  # exceptions are a human call, via handoff
                return Verdict("deny", chk.reason)
            if action == "issue_refund" and float(args["amount"]) > ent["refund_limit_usd"]:
                return Verdict("needs_human_approval", "above_tier_limit")
            return Verdict("needs_user_confirmation")
        if action in ("get_order_status", "search_policy", "escalate_to_human"):
            return Verdict("allow")
        return Verdict("deny", "unknown_action")
