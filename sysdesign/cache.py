"""Semantic cache for knowledge answers only, shared across replicas through Redis.

Rules enforced here (not left to callers):
- only intents in CACHEABLE_INTENTS are stored or served; prices, order
  status, refunds and anything user-specific never are;
- questions containing PII are never cached (they are user-specific);
- entries are namespaced by tenant + permission set, so one audience never
  reads another's answer;
- a similarity threshold, a TTL, and source-chunk tracking so a CDC change
  invalidates only entries built from the changed chunks.
"""
from __future__ import annotations

import hashlib
import json
import time

from .knowledge import cosine, embed
from .pii import contains_pii, redact

CACHEABLE_INTENTS = frozenset({"policy"})


class SemanticCache:
    def __init__(self, redis, threshold: float = 0.85, ttl_s: float = 3600, clock=None, prefix: str = "sc") -> None:
        if not 0.0 < threshold <= 1.0:
            raise ValueError("threshold must be in (0, 1]")
        self.r = redis
        self.threshold = threshold
        self.ttl_s = ttl_s
        self.clock = clock
        self.p = prefix
        self.stats = {"hits": 0, "misses": 0, "stores": 0, "refused": 0, "invalidated": 0}

    def _now(self) -> float:
        return self.clock.now() if self.clock else time.time()

    def namespace(self, tenant: str, permissions) -> str:
        perms = ",".join(sorted(permissions))
        return f"{self.p}:{tenant}:{hashlib.sha256(perms.encode()).hexdigest()[:12]}"

    def cacheable(self, intent: str, question: str) -> bool:
        return intent in CACHEABLE_INTENTS and not contains_pii(question)

    def get(self, tenant: str, permissions, intent: str, question: str):
        if not self.cacheable(intent, question):
            return None
        ns = self.namespace(tenant, permissions)
        q = embed(question)
        now = self._now()
        best, best_score, expired = None, -1.0, []
        for eid, raw in self.r.hgetall(ns).items():
            e = json.loads(raw)
            if now - e["created"] > self.ttl_s:
                expired.append(eid)
                continue
            s = cosine(q, e["vec"])
            if s > best_score:
                best, best_score = e, s
        if expired:
            self.r.hdel(ns, *expired)
        if best is not None and best_score >= self.threshold:
            self.stats["hits"] += 1
            return {"answer": best["answer"], "score": round(best_score, 4), "sources": best["sources"]}
        self.stats["misses"] += 1
        return None

    def put(self, tenant: str, permissions, intent: str, question: str, answer: str, sources: list[str]) -> bool:
        if not self.cacheable(intent, question) or not sources:
            self.stats["refused"] += 1
            return False
        ns = self.namespace(tenant, permissions)
        eid = hashlib.sha256(question.lower().encode()).hexdigest()[:16]
        entry = {"vec": embed(question), "answer": redact(answer), "sources": sorted(sources),
                 "created": self._now(), "q": redact(question)}
        self.r.hset(ns, eid, json.dumps(entry))
        self.stats["stores"] += 1
        return True

    def invalidate_chunks(self, chunk_ids: set[str]) -> int:
        """Drop only entries whose sources intersect the changed chunks (all namespaces)."""
        if not chunk_ids:
            return 0
        removed = 0
        for ns in self.r.scan_iter(match=f"{self.p}:*"):
            for eid, raw in self.r.hgetall(ns).items():
                if set(json.loads(raw)["sources"]) & chunk_ids:
                    self.r.hdel(ns, eid)
                    removed += 1
        self.stats["invalidated"] += removed
        return removed

    def size(self) -> int:
        return sum(self.r.hlen(ns) for ns in self.r.scan_iter(match=f"{self.p}:*"))
