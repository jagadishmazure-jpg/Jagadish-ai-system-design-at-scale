"""Approval gate for high-risk writes.

The model (planner) only PROPOSES. Deterministic code then checks ownership, permissions, business
policy and required fields; the user must confirm the exact action (bound by a digest of the
action); and the checks run AGAIN at execution time, so nothing the planner or a stale proposal
says is trusted. Business rules live here in code, not in a prompt.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass

from .tools import DAY, NOW, ORDERS, Registry

RETURN_WINDOW_DAYS = 30
DAMAGE_WINDOW_DAYS = 14
REQUIRED = {"issue_refund": ("order_id", "amount", "reason"), "request_cancellation": ("order_id",)}


@dataclass
class Check:
    ok: bool
    reason: str = ""


def digest(action: str, args: dict) -> str:
    return hashlib.sha256(json.dumps([action, args], sort_keys=True).encode()).hexdigest()[:16]


def policy_check(action: str, args: dict, identity: dict, now: float = NOW, store=None) -> Check:
    if action not in REQUIRED:
        return Check(False, "unknown_action")
    missing = [f for f in REQUIRED[action] if args.get(f) in (None, "")]
    if missing:
        return Check(False, "missing:" + ",".join(missing))
    order = ORDERS.get(args["order_id"])
    if order is None or order["owner"] != identity.get("sub"):
        return Check(False, "not_owner")
    if "chat" not in identity.get("scope", "").split():
        return Check(False, "forbidden")
    if action == "request_cancellation":
        return Check(True) if order["status"] in ("processing",) else Check(False, "not_cancellable")
    # refund rules
    if store is not None and store.effects(f"refund:{args['order_id']}"):
        return Check(False, "already_refunded")
    amount = args["amount"]
    if not isinstance(amount, (int, float)) or isinstance(amount, bool) or amount != amount or amount <= 0:
        return Check(False, "bad_amount")
    if round(float(amount), 2) > order["total"]:
        return Check(False, "amount_exceeds_order_total")
    if order["status"] != "delivered" or order["delivered_at"] is None:
        return Check(False, "not_delivered")
    if order["final_sale"]:
        return Check(False, "final_sale")
    age_days = (now - order["delivered_at"]) / DAY
    window = DAMAGE_WINDOW_DAYS if args["reason"] == "damaged" else RETURN_WINDOW_DAYS
    if args["reason"] == "damaged" and age_days > DAMAGE_WINDOW_DAYS and age_days <= RETURN_WINDOW_DAYS:
        window = RETURN_WINDOW_DAYS  # still inside the normal return window
    if age_days > window:
        return Check(False, "outside_window")
    return Check(True)


class ApprovalGate:
    """Agent proposes -> PolicyEngine decides -> (user confirms | human approves) -> execute.
    Checks re-run at execution time; each proposal executes at most once."""

    def __init__(self, registry: Registry, store, clock=None) -> None:
        from .policy import PolicyEngine
        self.registry = registry
        self.store = store
        self.policy = PolicyEngine(store)
        self.clock = clock

    def _save(self, pid: str, prop: dict, status: str) -> None:
        self.store.save_checkpoint(f"approval:{pid}", "approval", prop["owner"], status, prop, status)

    def propose(self, workflow_id: str, action: str, args: dict, identity: dict) -> dict:
        v = self.policy.decide(action, args, identity)
        if v.decision == "deny":
            return {"status": "rejected", "reason": v.reason}
        if v.decision == "allow":
            return {"status": "allowed"}
        pid = uuid.uuid4().hex[:12]
        status = "pending_confirmation" if v.decision == "needs_user_confirmation" else "pending_human"
        prop = {"proposal_id": pid, "action": action, "args": args, "owner": identity["sub"],
                "identity": {k: identity.get(k) for k in ("sub", "tenant", "scope", "tier")},
                "digest": digest(action, args), "workflow_id": workflow_id,
                "created": self.clock.wall() if self.clock else 0.0}
        self._save(pid, prop, status)
        return {"status": status, "proposal_id": pid, "confirm_digest": prop["digest"],
                "summary": describe(action, args), "reason": v.reason}

    async def _execute(self, pid: str, prop: dict) -> dict:
        ident = prop["identity"]
        v = self.policy.decide(prop["action"], prop["args"], ident)     # re-check at execution time
        if v.decision == "deny":
            self._save(pid, prop, "rejected")
            return {"status": "rejected", "reason": v.reason}
        if not self.store.record_effect_once(f"approval-exec:{pid}", {"by": ident["sub"]}):
            return {"status": "executed", "reason": "already_executed"}
        res = await self.registry.call(prop["action"], prop["args"], ident, _approved=True)
        status = "executed" if res.ok else "failed"
        self._save(pid, {**prop, "result": res.output, "error": res.error}, status)
        return {"status": status, "output": res.output, "error": res.error}

    def _load(self, pid: str):
        cp = self.store.load_checkpoint(f"approval:{pid}")
        return (cp, cp["state"]) if cp else (None, None)

    async def confirm(self, proposal_id: str, confirm_digest: str, identity: dict) -> dict:
        cp, prop = self._load(proposal_id)
        if cp is None or cp["owner"] != identity.get("sub"):
            return {"status": "rejected", "reason": "unknown_proposal"}
        if cp["status"] != "pending_confirmation":
            return {"status": cp["status"], "reason": "not_awaiting_user"}
        if confirm_digest != prop["digest"]:
            return {"status": "rejected", "reason": "confirmation_does_not_match_action"}
        return await self._execute(proposal_id, prop)

    async def human_approve(self, proposal_id: str, approver: dict, approve: bool = True) -> dict:
        """Only a different person holding the 'approver' scope can approve; never the requester."""
        cp, prop = self._load(proposal_id)
        if cp is None or cp["status"] != "pending_human":
            return {"status": "rejected", "reason": "not_pending_human"}
        if "approver" not in (approver.get("scope") or "").split() or approver.get("sub") == prop["owner"]:
            return {"status": "rejected", "reason": "approver_not_authorised"}
        if not approve:
            self._save(proposal_id, prop, "declined")
            return {"status": "declined"}
        # the human decision replaces the tier limit, nothing else: ownership/window still re-checked
        prop = {**prop, "human_approved_by": approver["sub"]}
        ident = prop["identity"]
        chk = policy_check(prop["action"], prop["args"], ident, store=self.store)
        if not chk.ok:
            self._save(proposal_id, prop, "rejected")
            return {"status": "rejected", "reason": chk.reason}
        if not self.store.record_effect_once(f"approval-exec:{proposal_id}", {"by": approver["sub"]}):
            return {"status": "executed", "reason": "already_executed"}
        res = await self.registry.call(prop["action"], prop["args"], ident, _approved=True)
        status = "executed" if res.ok else "failed"
        self._save(proposal_id, {**prop, "result": res.output}, status)
        return {"status": status, "output": res.output, "error": res.error}

    def decline(self, proposal_id: str, identity: dict) -> dict:
        cp, prop = self._load(proposal_id)
        if cp is None or cp["owner"] != identity.get("sub") or cp["status"] not in ("pending_confirmation",
                                                                                    "pending_human"):
            return {"status": "rejected", "reason": "unknown_proposal"}
        self._save(proposal_id, prop, "declined")
        return {"status": "declined"}


def describe(action: str, args: dict) -> str:
    if action == "issue_refund":
        return f"Refund ${args['amount']:.2f} for order {args['order_id']} (reason: {args['reason']})."
    return f"Send a cancellation request for order {args['order_id']} to our team."
