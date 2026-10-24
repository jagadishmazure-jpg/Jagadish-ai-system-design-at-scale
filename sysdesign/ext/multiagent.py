"""Optional multi-agent coordination layer (kept small on purpose; see ADR 0003).

Specialists publish agent cards (name, capabilities, input contract). The orchestrator discovers
them at runtime, hands off a task, and every specialist updates one shared task record
(pending -> in_progress -> done | failed). Status updates are idempotent and never move backwards.
Three local specialists: retrieval, citation checking and calculation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .calc import refund_amount, tax
from .rag import KnowledgeBase, tokens

ORDER = {"pending": 0, "in_progress": 1, "done": 2, "failed": 2}


@dataclass
class AgentCard:
    name: str
    capabilities: list[str]
    description: str
    input_fields: list[str]


class TaskBoard:
    def __init__(self) -> None:
        self.tasks: dict[str, dict] = {}
        self.updates = 0

    def create(self, task_id: str, capability: str, payload: dict) -> dict:
        return self.tasks.setdefault(task_id, {"task_id": task_id, "capability": capability, "payload": payload,
                                               "status": "pending", "owner": None, "result": None})

    def update(self, task_id: str, status: str, owner: str, result=None) -> bool:
        t = self.tasks[task_id]
        if t["status"] == status and t["owner"] == owner:
            return False                       # idempotent repeat
        if ORDER[status] < ORDER[t["status"]] or t["status"] in ("done", "failed"):
            return False                       # never move backwards or reopen a finished task
        t.update(status=status, owner=owner, result=result)
        self.updates += 1
        return True


class RetrievalAgent:
    card = AgentCard("retrieval", ["retrieve_policy"], "Finds policy passages for a question.", ["question"])

    def __init__(self, kb: KnowledgeBase) -> None:
        self.kb = kb

    def run(self, payload: dict) -> dict:
        return {"sources": self.kb.search(payload["question"], k=3, min_score=0.2)}


class CitationAgent:
    """Each claim must share enough content words with at least one retrieved source."""
    card = AgentCard("citation", ["check_citations"], "Verifies each claim maps to a retrieved source.",
                     ["claims", "sources"])

    def run(self, payload: dict, min_overlap: float = 0.6) -> dict:
        results = []
        for claim in payload["claims"]:
            ct = set(tokens(claim))
            nums = set(re.findall(r"\d+", claim))
            best, src = 0.0, None
            for s in payload["sources"]:
                st = set(tokens(s["text"]))
                ov = len(ct & st) / max(1, len(ct))
                if nums and not nums <= set(re.findall(r"\d+", s["text"])):
                    ov = 0.0                      # a number not in the source is unsupported
                if ov > best:
                    best, src = ov, s["id"]
            results.append({"claim": claim, "supported": best >= min_overlap, "source": src if best >= min_overlap
                            else None, "overlap": round(best, 2)})
        return {"claims": results, "all_supported": all(r["supported"] for r in results)}


class CalcAgent:
    card = AgentCard("calculator", ["calc_refund", "calc_tax"], "Deterministic money math.", ["op", "args"])

    def run(self, payload: dict) -> dict:
        if payload["op"] == "calc_refund":
            return {"amount": refund_amount(**payload["args"])}
        return {"amount": tax(**payload["args"])}


@dataclass
class Orchestrator:
    agents: list = field(default_factory=list)
    board: TaskBoard = field(default_factory=TaskBoard)
    fallback_log: list = field(default_factory=list)

    def discover(self) -> dict[str, str]:
        return {cap: a.card.name for a in self.agents for cap in a.card.capabilities}

    def handoff(self, task_id: str, capability: str, payload: dict) -> dict:
        self.board.create(task_id, capability, payload)
        owner = next((a for a in self.agents if capability in a.card.capabilities), None)
        if owner is None:
            self.fallback_log.append(capability)
            self.board.update(task_id, "failed", "orchestrator", {"fallback": "human_queue"})
            return self.board.tasks[task_id]
        self.board.update(task_id, "in_progress", owner.card.name)
        try:
            result = owner.run(payload)
        except (KeyError, ValueError, TypeError) as e:
            self.board.update(task_id, "failed", owner.card.name, {"error": type(e).__name__})
            return self.board.tasks[task_id]
        self.board.update(task_id, "done", owner.card.name, result)
        return self.board.tasks[task_id]
