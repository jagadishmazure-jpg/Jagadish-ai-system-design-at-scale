"""The approved-refund workflow as runtime steps with Saga compensations.

verify -> await_approval -> issue_refund -> create_return_label -> schedule_pickup
Each side effect uses an idempotency key derived from (workflow id, step), so a retry or a resume on
another replica never repeats it. If schedule_pickup fails for good, the label is voided and the
refund reversed, in that (reverse) order.
"""
from __future__ import annotations

from .runtime import Runtime, StepDef, StepFailed
from .tools import ORDERS


def refund_flow(store, clock, *, pickup_fails: bool = False, kill_after: str | None = None,
                replica: str = "api-1", approval_required: bool = True) -> Runtime:
    log = store  # all effects go through store.record_effect_once

    async def verify(st):
        o = ORDERS.get(st["inputs"]["order_id"])
        if o is None or o["owner"] != st["inputs"]["owner"]:
            raise StepFailed("not owner")
        return {"total": o["total"], "tokens": 0}

    async def issue(st):
        k = f"wf:{st['workflow_id']}:refund"
        created = log.record_effect_once(k, {"amount": st["results"]["verify"]["total"]})
        return {"refund_id": "RF-" + st["workflow_id"][-6:], "created": created}

    async def undo_issue(st):
        log.record_effect_once(f"wf:{st['workflow_id']}:refund_reversal", {"of": "refund"})

    async def label(st):
        created = log.record_effect_once(f"wf:{st['workflow_id']}:label", {"carrier": "local-courier"})
        return {"label_id": "LB-" + st["workflow_id"][-6:], "created": created}

    async def undo_label(st):
        log.record_effect_once(f"wf:{st['workflow_id']}:label_void", {"of": "label"})

    async def pickup(st):
        if pickup_fails:
            raise ConnectionError("courier API down")
        log.record_effect_once(f"wf:{st['workflow_id']}:pickup", {})
        return {"pickup": "booked"}

    async def wait(st):
        return {"approval": st.get("approval")}

    steps = [StepDef("verify", verify),
             *([StepDef("await_approval", wait, waits_for_approval=True)] if approval_required else []),
             StepDef("issue_refund", issue, undo_issue, max_attempts=1),
             StepDef("create_return_label", label, undo_label),
             StepDef("schedule_pickup", pickup, max_attempts=3)]
    return Runtime(store, clock, steps, replica=replica, kill_after=kill_after)
