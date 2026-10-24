"""Checkpointed refund workflow: each step commits before the next, so a replica crash mid-workflow
resumes on another replica from the last committed step and the refund is issued exactly once."""
from __future__ import annotations

from .guardrails import PolicyViolation
from .tools import NotFound

STEPS = ("verify_order", "check_policy", "issue_refund", "notify")


class Crash(Exception):
    """Injected to simulate a replica dying between steps."""


def run_refund(store, commerce, workflow_id: str, user_id: str, order_id: str, amount: float,
               worker: str, crash_after: str | None = None) -> dict:
    cp = store.load(workflow_id)
    if cp and cp["status"] in ("done", "rejected"):
        return cp
    step = cp["step"] if cp else 0
    state = cp["state"] if cp else {"order_id": order_id, "amount": amount, "user_id": user_id, "log": []}
    while step < len(STEPS):
        name = STEPS[step]
        try:
            if name == "verify_order":
                commerce.order_status(state["order_id"], state["user_id"])
            elif name == "check_policy":
                o = commerce.orders[state["order_id"]]
                if state["amount"] + commerce.refunded_total(state["order_id"]) > o["total"]:
                    raise PolicyViolation("refund exceeds order total")
            elif name == "issue_refund":
                rec = commerce.issue_refund(state["order_id"], state["user_id"], state["amount"],
                                            idempotency_key=f"wf:{workflow_id}")
                state["refund_id"] = rec["refund_id"]
            elif name == "notify":
                state["notified"] = True
        except (PolicyViolation, NotFound) as e:
            state["error"] = str(e)
            store.save(workflow_id, step, state, "rejected", worker)
            return store.load(workflow_id)
        state["log"].append(f"{name}@{worker}")
        step += 1
        store.save(workflow_id, step, state, "running" if step < len(STEPS) else "done", worker)
        if crash_after == name:
            raise Crash(name)
    return store.load(workflow_id)
