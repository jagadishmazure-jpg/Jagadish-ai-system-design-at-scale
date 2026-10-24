"""Semantic cache for knowledge answers only.

Rules (enforced here, not by convention):
- only intents in CACHEABLE_INTENTS are stored; never prices, user-specific data or tool actions,
- the key space is partitioned by tenant AND permission scope, so one user's answer can never be
  served to a caller with different rights,
- lookups need cosine similarity >= threshold, entries expire after ttl_s,
- each entry records the chunk ids it was built from; invalidate_chunks() drops only those,
- queries are PII-redacted before they are stored, and answers containing PII are not stored.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .clock import RealClock
from .pii import contains_pii, redact
from .rag import cosine, embed

CACHEABLE_INTENTS = {"faq_policy"}
PRICE = re.compile(r"\$\s?\d|\bprice\b|\bcost[s]?\b", re.IGNORECASE)
ORDER_REF = re.compile(r"\bQM-\d{5}\b")


@dataclass
class Entry:
    query: str
    vec: list[float]
    answer: str
    chunk_ids: list[str]
    expires: float


@dataclass
class SemanticCache:
    threshold: float = 0.90
    ttl_s: float = 3600.0
    clock: object = field(default_factory=RealClock)
    enabled: bool = True
    store: dict[str, list[Entry]] = field(default_factory=dict)
    hits: int = 0
    misses: int = 0

    @staticmethod
    def partition(tenant: str, scopes: str) -> str:
        return f"{tenant}|{' '.join(sorted(scopes.split()))}"

    def cacheable(self, intent: str, query: str, answer: str | None = None, used_tools: bool = False) -> bool:
        if intent not in CACHEABLE_INTENTS or used_tools:
            return False
        for text in (query, answer or ""):
            if PRICE.search(text) or ORDER_REF.search(text) or contains_pii(text):
                return False
        return True

    def get(self, tenant: str, scopes: str, intent: str, query: str) -> Entry | None:
        if not self.enabled or not self.cacheable(intent, query):
            return None
        now = self.clock.now()
        part = self.store.get(self.partition(tenant, scopes), [])
        part[:] = [e for e in part if e.expires > now]
        q = embed(redact(query))
        best = max(part, key=lambda e: cosine(q, e.vec), default=None)
        if best is not None and cosine(q, best.vec) >= self.threshold:
            self.hits += 1
            return best
        self.misses += 1
        return None

    def put(self, tenant: str, scopes: str, intent: str, query: str, answer: str,
            chunk_ids: list[str], used_tools: bool = False) -> bool:
        if not self.enabled or not chunk_ids or not self.cacheable(intent, query, answer, used_tools):
            return False
        q = redact(query)
        self.store.setdefault(self.partition(tenant, scopes), []).append(
            Entry(q, embed(q), answer, list(chunk_ids), self.clock.now() + self.ttl_s))
        return True

    def invalidate_chunks(self, chunk_ids: list[str]) -> int:
        ids, dropped = set(chunk_ids), 0
        for part in self.store.values():
            keep = [e for e in part if not ids & set(e.chunk_ids)]
            dropped += len(part) - len(keep)
            part[:] = keep
        return dropped

    def size(self) -> int:
        return sum(len(p) for p in self.store.values())

    def hit_rate(self) -> float:
        n = self.hits + self.misses
        return self.hits / n if n else 0.0
