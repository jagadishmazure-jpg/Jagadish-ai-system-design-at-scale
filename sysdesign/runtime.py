"""Workflow runtime: the runtime, not the model, owns execution.

Responsibilities: per-step checkpoints with completion flags, idempotency keys for side effects,
a checksum over the saved state (a corrupted or hand-edited checkpoint is refused), bounded
retries and timeouts per step, token and wall-time budgets, cancellation, suspension while a
human approval is pending (hours are fine: nothing is held in memory), resume on any replica after
a crash or restart, and Saga compensation: when a step fails for good, completed steps are undone
in reverse order.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field


class InstanceKilled(Exception):
    """Raised by the chaos hook to simulate a replica dying mid-workflow."""


class StepFailed(Exception):
    pass


class BudgetExceeded(Exception):
    pass


@dataclass
class StepDef:
    name: str
    run: Callable[[dict], Awaitable[dict]]
    compensate: Callable[[dict], Awaitable[None]] | None = None
    timeout_s: float = 1.0
    max_attempts: int = 2
    waits_for_approval: bool = False


@dataclass
class Budget:
    max_tokens: int = 20_000
    max_wall_s: float = 3600 * 48   # a human approval may take hours


def checksum(state: dict) -> str:
    body = {k: v for k, v in state.items() if k != "_sum"}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


@dataclass
class Runtime:
    store: object
    clock: object
    steps: list[StepDef]
    kind: str = "refund_flow"
    budget: Budget = field(default_factory=Budget)
    replica: str = "api-1"
    kill_after: str | None = None      # chaos: die right after this step's checkpoint

    def _save(self, wid: str, owner: str, state: dict, status: str) -> None:
        state["_sum"] = checksum(state)
        self.store.save_checkpoint(wid, self.kind, owner, state.get("step", ""), state, status)

    def _load(self, wid: str) -> tuple[dict, str] | None:
        cp = self.store.load_checkpoint(wid)
        if cp is None:
            return None
        if cp["state"].get("_sum") != checksum(cp["state"]):
            raise StepFailed("checkpoint checksum mismatch")
        return cp["state"], cp["status"]

    def start(self, wid: str, owner: str, inputs: dict) -> dict:
        loaded = self._load(wid)
        if loaded:  # idempotent start: same workflow id never runs twice from scratch
            return loaded[0]
        state = {"inputs": inputs, "done": {}, "results": {}, "tokens": 0, "started": self.clock.wall(),
                 "step": self.steps[0].name, "attempts": {}, "replicas": [], "compensated": []}
        self._save(wid, owner, state, "running")
        return state

    def cancel(self, wid: str, owner: str) -> None:
        state, status = self._load(wid)
        if status in ("completed", "compensated", "cancelled"):
            return
        state["cancel_requested"] = True
        self._save(wid, owner, state, status)

    def approve(self, wid: str, owner: str, approved: bool) -> None:
        state, status = self._load(wid)
        if status != "waiting_approval":
            return
        state["approval"] = "approved" if approved else "declined"
        self._save(wid, owner, state, "running")

    async def run(self, wid: str, owner: str) -> str:
        state, status = self._load(wid)
        if status in ("completed", "compensated", "cancelled", "failed"):
            return status
        state["replicas"].append(self.replica)
        for step in self.steps:
            if state["done"].get(step.name):
                continue                                     # completion flag: skip finished work
            if state.get("cancel_requested"):
                return await self._compensate(wid, owner, state, "cancelled")
            if self.clock.wall() - state["started"] > self.budget.max_wall_s:
                return await self._compensate(wid, owner, state, "failed", "wall_budget")
            if state["tokens"] > self.budget.max_tokens:
                return await self._compensate(wid, owner, state, "failed", "token_budget")
            state["step"] = step.name
            if step.waits_for_approval:
                if state.get("approval") is None:
                    self._save(wid, owner, state, "waiting_approval")
                    return "waiting_approval"           # suspended; nothing held in memory
                if state["approval"] == "declined":
                    return await self._compensate(wid, owner, state, "cancelled", "declined")
            ok = False
            for _ in range(step.max_attempts):
                state["attempts"][step.name] = state["attempts"].get(step.name, 0) + 1
                try:
                    out = await asyncio.wait_for(step.run({**state, "workflow_id": wid}), step.timeout_s)
                    state["results"][step.name] = out
                    state["tokens"] += int(out.get("tokens", 0)) if isinstance(out, dict) else 0
                    ok = True
                    break
                except (TimeoutError, StepFailed, ConnectionError) as e:
                    state["last_error"] = f"{step.name}: {type(e).__name__}"
            if not ok:
                return await self._compensate(wid, owner, state, "compensated", state.get("last_error"))
            state["done"][step.name] = True
            self._save(wid, owner, state, "running")
            if self.kill_after == step.name:
                raise InstanceKilled(self.replica)
        state["step"] = "end"
        self._save(wid, owner, state, "completed")
        return "completed"

    async def _compensate(self, wid: str, owner: str, state: dict, final: str, why: str | None = None) -> str:
        for step in reversed(self.steps):
            if state["done"].get(step.name) and step.compensate and step.name not in state["compensated"]:
                await step.compensate({**state, "workflow_id": wid})
                state["compensated"].append(step.name)
                self._save(wid, owner, state, "compensating")
        state["reason"] = why
        self._save(wid, owner, state, final)
        return final

    def status(self, wid: str) -> str | None:
        cp = self.store.load_checkpoint(wid)
        return cp["status"] if cp else None
