"""Policy/FAQ retrieval with CDC-style incremental re-indexing.

Embeddings are a deterministic hashed bag of words (no model download) so tests are stable.
Each chunk carries a content hash and version; apply_changes() re-embeds only chunks whose
hash changed and returns their ids so the semantic cache can drop only dependent entries.
"""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field

DIM = 512
STOP = {"the", "a", "an", "is", "are", "to", "of", "and", "or", "for", "in", "on", "my", "i", "do",
        "can", "how", "what", "you", "your", "it", "me", "with", "be", "does", "if", "at", "when", "we"}


def _stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if w.endswith(suf) and len(w) > len(suf) + 2:
            return w[: -len(suf)]
    return w


STOP |= {"long", "take", "much", "get", "will", "would", "have", "want", "item", "until", "up", "this", "that", "there", "please", "about", "any", "so", "no"}
SYN = {"ship": "ship", "shipp": "ship", "deliver": "ship", "delivery": "ship", "internationally": "international",
       "abroad": "international",
       "show": "appear", "broken": "damag", "defective": "damag", "damage": "damag"}


def tokens(text: str) -> list[str]:
    out = []
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        if w in STOP:
            continue
        t = _stem(w)
        out.append(SYN.get(t, SYN.get(w, t)))
    return out


def embed(text: str) -> list[float]:
    v = [0.0] * DIM
    for t in tokens(text):
        h = int(hashlib.md5(t.encode(), usedforsecurity=False).hexdigest(), 16)
        v[h % DIM] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=False))


POLICY_DOCS = [
    ("returns-window", "Returns: items can be returned within 30 days of delivery for a full refund "
                       "to the original payment method. Final-sale items cannot be returned."),
    ("returns-damaged", "Damaged or defective items: report within 14 days with a photo. We ship a "
                        "replacement or refund the item price plus original shipping."),
    ("shipping-times", "Shipping: standard delivery takes 3 to 5 business days, express takes 1 to 2 "
                       "business days. Orders placed before 2 pm ship the same day."),
    ("shipping-intl", "International shipping is available to 40 countries. Duties and taxes are "
                      "collected at checkout so there are no surprise fees on delivery."),
    ("refund-timing", "Refund timing: once a return is received, refunds are issued within 5 business "
                      "days. Card refunds can take a further 3 to 10 days to appear."),
    ("price-match", "Price adjustments: we do not offer price matching or retroactive discounts. "
                    "Support agents cannot create discount codes."),
    ("account-help", "Account: you can change your delivery address until the order is packed. "
                     "Password resets are done from the sign-in page."),
    ("warranty", "Warranty: electronics carry a 12 month limited warranty covering manufacturing "
                 "defects, not accidental damage."),
]


@dataclass
class Chunk:
    id: str
    text: str
    version: int = 1
    vec: list[float] = field(default_factory=list)
    sha: str = ""


class KnowledgeBase:
    def __init__(self, docs=POLICY_DOCS) -> None:
        self.chunks: dict[str, Chunk] = {}
        self.embed_calls = 0
        for cid, text in docs:
            self._put(cid, text, 1)

    def _put(self, cid: str, text: str, version: int) -> None:
        self.embed_calls += 1
        self.chunks[cid] = Chunk(cid, text, version, embed(text), hashlib.sha256(text.encode()).hexdigest())

    def _idf(self) -> dict[str, float]:
        df: dict[str, int] = {}
        for c in self.chunks.values():
            for t in set(tokens(c.text)):
                df[t] = df.get(t, 0) + 1
        n = max(1, len(self.chunks))
        return {t: math.log(1 + n / d) for t, d in df.items()}

    def _score(self, q_terms: list[str], doc_terms: list[str], idf: dict[str, float], unk: float) -> float:
        """IDF-weighted cosine over term sets. Query words the corpus has never seen keep a high
        weight, so a junk question that shares one common word scores low (honest "don't know")."""
        qw = {t: idf.get(t, unk) for t in set(q_terms)}
        dw = {t: idf.get(t, unk) for t in set(doc_terms)}
        dot = sum(w * dw[t] for t, w in qw.items() if t in dw)
        nq = math.sqrt(sum(w * w for w in qw.values())) or 1.0
        nd = math.sqrt(sum(w * w for w in dw.values())) or 1.0
        return dot / (nq * nd)

    def search(self, query: str, k: int = 2, min_score: float = 0.15) -> list[dict]:
        idf = self._idf()
        unk = max(idf.values(), default=1.0)
        qt = tokens(query)
        scored = sorted(((self._score(qt, tokens(c.text), idf, unk), c) for c in self.chunks.values()),
                        key=lambda x: -x[0])
        return [{"id": c.id, "text": c.text, "version": c.version, "score": round(s, 3)}
                for s, c in scored[:k] if s >= min_score]

    def apply_changes(self, events: list[dict]) -> list[str]:
        """CDC events: {"op": "upsert"|"delete", "id": ..., "text": ...}. Returns changed ids."""
        changed = []
        for ev in events:
            cid = ev["id"]
            if ev["op"] == "delete":
                if self.chunks.pop(cid, None) is not None:
                    changed.append(cid)
                continue
            new_sha = hashlib.sha256(ev["text"].encode()).hexdigest()
            old = self.chunks.get(cid)
            if old is not None and old.sha == new_sha:
                continue  # no-op update: do not re-embed, do not invalidate
            self._put(cid, ev["text"], (old.version + 1) if old else 1)
            changed.append(cid)
        return changed
