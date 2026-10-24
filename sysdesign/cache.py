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


# --------------------------------------------------------------------------
# Talk 15 addition: exact-match response cache (cache-aside, 24h TTL).
# Sits in front of SemanticCache: a cheap exact lookup first, the similarity
# search only on a miss. Same intent and PII rules as SemanticCache.

_EDGE_PUNCT = "?!.,;:\"'()[]{}"
_SENTENCE_END = "?!.,;:"


def normalize_prompt(text: str) -> str:
    """Casefold, collapse whitespace and drop edge punctuation from plain words.

    A token containing a digit (order numbers, SKUs, account ids) keeps its case
    and inner punctuation; only a trailing sentence mark is removed, so
    "#A-1042?" and "#A-1042" match but "#A-1042" and "#A-1043" never do.
    """
    out = []
    for tok in text.split():
        if any(ch.isdigit() for ch in tok):
            tok = tok.rstrip(_SENTENCE_END)
        else:
            tok = tok.strip(_EDGE_PUNCT).casefold()
        if tok:
            out.append(tok)
    return " ".join(out)


class ResponseCache:
    """Cache-aside: the reader checks the cache, computes on a miss, then writes."""

    def __init__(self, redis, ttl_s: int = 24 * 3600, prefix: str = "rc") -> None:
        if ttl_s <= 0:
            raise ValueError("ttl_s must be > 0")
        self.r, self.ttl_s, self.p = redis, ttl_s, prefix
        self.stats = {"hits": 0, "misses": 0, "stores": 0, "refused": 0}

    def key(self, tenant: str, prompt: str) -> str:
        h = hashlib.sha256(normalize_prompt(prompt).encode()).hexdigest()[:24]
        return f"{self.p}:{tenant}:{h}"

    def get_or_compute(self, tenant: str, intent: str, prompt: str, compute) -> tuple[str, bool]:
        if intent not in CACHEABLE_INTENTS or contains_pii(prompt):
            self.stats["refused"] += 1
            return compute(), False
        k = self.key(tenant, prompt)
        raw = self.r.get(k)
        if raw is not None:
            self.stats["hits"] += 1
            return raw.decode() if isinstance(raw, bytes) else raw, True
        self.stats["misses"] += 1
        answer = compute()
        self.r.set(k, redact(answer), ex=self.ttl_s)
        self.stats["stores"] += 1
        return answer, False
