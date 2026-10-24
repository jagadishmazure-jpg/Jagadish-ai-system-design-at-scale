"""Policy knowledge base for a fictional retailer, chunked and indexed with a tiny local embedding.

The embedding is a hashed bag of word-stems (no model download); good enough
to demonstrate retrieval, similarity thresholds and change-data-capture
re-indexing. A CDC event re-embeds only chunks whose content hash changed
and reports exactly which chunk ids changed so the cache can invalidate them.
"""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass

DIM = 512
_STOP = set("a an the is are was were do does did i my me you your to of for on in at and or it this that can "
            "how what when with be by if am will would should could please".split())


def _stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > 4 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def tokens(text: str) -> list[str]:
    return [_stem(w) for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOP]


def embed(text: str) -> list[float]:
    v = [0.0] * DIM
    for t in tokens(text):
        h = int(hashlib.blake2b(t.encode(), digest_size=4).hexdigest(), 16)
        v[h % DIM] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


POLICY_DOCS = {
    "returns": [
        "Electronics can be returned within 30 days of delivery in original packaging.",
        "Clothing and shoes can be returned within 45 days if unworn with tags attached.",
        "Refunds go back to the original payment method within 5 business days of the return scan.",
    ],
    "payments": [
        "Cash on delivery is available for orders under 500 dollars in supported postal areas.",
        "Gift cards cannot be exchanged for cash and do not expire.",
    ],
    "shipping": [
        "Standard shipping takes 3 to 5 business days and is free on orders over 35 dollars.",
        "Express shipping arrives in 1 to 2 business days for a flat 12 dollar fee.",
    ],
    "discounts": [
        "Support agents may offer a goodwill credit of at most 10 percent of the order value, once per order.",
    ],
}


@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    text: str
    content_hash: str
    vector: list[float]


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


class KnowledgeIndex:
    def __init__(self, docs: dict | None = None) -> None:
        self.chunks: dict[str, Chunk] = {}
        self.embed_calls = 0
        for doc_id, paras in (docs or POLICY_DOCS).items():
            self.apply_change(doc_id, paras)
        self.embed_calls = 0  # count only re-index work after startup

    def apply_change(self, doc_id: str, paragraphs: list[str]) -> set[str]:
        """CDC handler: upsert changed chunks, delete removed ones; return changed chunk ids."""
        changed: set[str] = set()
        new_ids = set()
        for i, text in enumerate(paragraphs):
            cid = f"{doc_id}#{i}"
            new_ids.add(cid)
            h = _hash(text)
            old = self.chunks.get(cid)
            if old is None or old.content_hash != h:
                self.embed_calls += 1
                self.chunks[cid] = Chunk(cid, doc_id, text, h, embed(text))
                changed.add(cid)
        for cid in [c for c, ch in self.chunks.items() if ch.doc_id == doc_id and c not in new_ids]:
            del self.chunks[cid]
            changed.add(cid)
        return changed

    def search(self, query: str, k: int = 2, min_score: float = 0.15) -> list[tuple[Chunk, float]]:
        q = embed(query)
        scored = sorted(((c, cosine(q, c.vector)) for c in self.chunks.values()), key=lambda x: -x[1])
        return [(c, s) for c, s in scored[:k] if s >= min_score]
