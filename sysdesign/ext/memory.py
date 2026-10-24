"""Memory, split by purpose:
- knowledge         : shared policy corpus (rag.KnowledgeBase / knowledge.KnowledgeRouter)
- decision context  : per-decision field funnel (context.funnel), rebuilt every time
- episodic memory   : similar resolved past cases, retrieved by similarity, advisory only
- facts             : user facts with valid_from / expires_at; stale facts are never returned
- workflow state    : exact checkpoints in store.Store (runtime.py), the only thing used to resume
Nothing in memory can authorise an action; the policy layer reads verified data only.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..pii import redact
from .rag import cosine, embed


@dataclass
class Episode:
    case_id: str
    summary: str
    resolution: str
    vec: list[float] = field(default_factory=list)


class EpisodicMemory:
    def __init__(self) -> None:
        self.cases: list[Episode] = []

    def add(self, case_id: str, summary: str, resolution: str) -> None:
        s = redact(summary)
        self.cases.append(Episode(case_id, s, resolution, embed(s)))

    def similar(self, query: str, k: int = 2, min_score: float = 0.3) -> list[dict]:
        q = embed(redact(query))
        scored = sorted(((cosine(q, c.vec), c) for c in self.cases), key=lambda x: -x[0])
        return [{"case_id": c.case_id, "resolution": c.resolution, "score": round(s, 3)}
                for s, c in scored[:k] if s >= min_score]


@dataclass
class Fact:
    key: str
    value: str
    valid_from: float
    expires_at: float | None
    source: str


class FactMemory:
    def __init__(self, clock) -> None:
        self.clock = clock
        self.facts: dict[tuple[str, str], Fact] = {}
        self.flags: dict[str, list[dict]] = {}   # shared conversation state: things a user flagged

    def put(self, user: str, key: str, value: str, ttl_s: float | None, source: str) -> None:
        now = self.clock.now()
        self.facts[(user, key)] = Fact(key, redact(value), now, now + ttl_s if ttl_s else None, source)

    def get(self, user: str, key: str) -> str | None:
        f = self.facts.get((user, key))
        now = self.clock.now()
        if f is None or now < f.valid_from or (f.expires_at is not None and now >= f.expires_at):
            return None
        return f.value

    def invalidate(self, user: str, key: str) -> None:
        self.facts.pop((user, key), None)

    def flag(self, user: str, order_id: str, note: str) -> None:
        self.flags.setdefault(user, []).append({"order_id": order_id, "note": redact(note), "at": self.clock.now()})

    def resolve_reference(self, user: str, text: str) -> str | None:
        """'the one I flagged last week' -> the most recent flagged order (within 14 days)."""
        t = text.lower()
        if "flagged" not in t and "the one i reported" not in t:
            return None
        now = self.clock.now()
        recent = [f for f in self.flags.get(user, []) if now - f["at"] <= 14 * 86400]
        return recent[-1]["order_id"] if recent else None
