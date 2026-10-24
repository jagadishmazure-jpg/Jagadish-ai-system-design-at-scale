"""Knowledge design: route a question by its STRUCTURE to the store that can answer it.

- relationship questions ("does X fit Y", "does the returns policy apply to Z") -> small graph
- aggregate / exact-record questions ("how many orders", "total of my orders")   -> SQL
- everything else (policy prose)                                                  -> vector search
A relational question sent to vector search alone gets a plausible but wrong chunk; tests show it.
Also: an optional lexical reranker, enabled only when evals show it pays for its latency.
"""
from __future__ import annotations

import re
import sqlite3
import time

from .rag import KnowledgeBase, tokens

# Fictional catalogue relationships.
EDGES = [
    ("canister-230g", "fits", "camp-stove-compact"),
    ("canister-450g", "fits", "camp-stove-compact"),
    ("canister-450g", "fits", "camp-stove-family"),
    ("rain-cover-40l", "fits", "trail-backpack-40l"),
    ("returns-window", "applies_to", "apparel"),
    ("returns-window", "applies_to", "gear"),
    ("returns-damaged", "applies_to", "electronics"),
    ("warranty", "applies_to", "electronics"),
]
ALIASES = {"small canister": "canister-230g", "230g canister": "canister-230g", "large canister": "canister-450g",
           "450g canister": "canister-450g", "compact stove": "camp-stove-compact", "family stove": "camp-stove-family",
           "rain cover": "rain-cover-40l", "40l backpack": "trail-backpack-40l", "trail backpack": "trail-backpack-40l",
           "return policy": "returns-window", "returns policy": "returns-window", "warranty": "warranty",
           "electronics": "electronics", "apparel": "apparel", "gear": "gear"}


class Graph:
    def __init__(self, edges=EDGES) -> None:
        self.edges = set(edges)

    def related(self, a: str, rel: str, b: str) -> bool:
        return (a, rel, b) in self.edges

    def neighbours(self, a: str, rel: str) -> list[str]:
        return sorted(b for x, r, b in self.edges if x == a and r == rel)


def entities(q: str) -> list[str]:
    t = q.lower()
    found = [(t.find(k), v) for k, v in ALIASES.items() if k in t]
    return [v for _, v in sorted(found)]


class OrdersSQL:
    """Structured lookups over a read-only, parameterised table. The model never writes SQL."""

    def __init__(self, orders: dict) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.execute("CREATE TABLE orders (order_id TEXT, owner TEXT, status TEXT, total REAL)")
        self.db.executemany("INSERT INTO orders VALUES (?,?,?,?)",
                            [(k, v["owner"], v["status"], v["total"]) for k, v in orders.items()])

    QUERIES = {
        "count_orders": "SELECT COUNT(*) FROM orders WHERE owner = ?",
        "sum_totals": "SELECT ROUND(COALESCE(SUM(total), 0), 2) FROM orders WHERE owner = ?",
        "count_in_transit": "SELECT COUNT(*) FROM orders WHERE owner = ? AND status = 'in_transit'",
    }

    def run(self, name: str, owner: str):
        return self.db.execute(self.QUERIES[name], (owner,)).fetchone()[0]


def classify_structure(q: str) -> str:
    t = q.lower()
    if re.search(r"\b(fit|fits|compatible|work with|apply to|applies to|cover)\b", t) and len(entities(q)) >= 2:
        return "graph"
    if re.search(r"\b(how many|count|total of|sum of|in total)\b", t) and "order" in t:
        return "sql"
    return "vector"


def rerank(query: str, hits: list[dict], cost_s: float = 0.004) -> list[dict]:
    """Lexical-overlap reranker (stands in for a cross-encoder). cost_s simulates its latency."""
    time.sleep(cost_s)
    q = set(tokens(query))
    return sorted(hits, key=lambda h: -(len(q & set(tokens(h["text"]))) + h["score"]))


class KnowledgeRouter:
    def __init__(self, kb: KnowledgeBase, orders: dict, use_reranker: bool = False) -> None:
        self.kb, self.graph, self.sql = kb, Graph(), OrdersSQL(orders)
        self.use_reranker = use_reranker

    def answer(self, q: str, owner: str) -> dict:
        kind = classify_structure(q)
        if kind == "graph":
            ents = entities(q)
            a, b = ents[0], ents[1]
            rel = "applies_to" if "appl" in q.lower() or a.startswith(("returns", "warranty")) else "fits"
            ok = self.graph.related(a, rel, b) or self.graph.related(b, rel, a)
            return {"source": "graph", "answer": "yes" if ok else "no", "facts": [a, rel, b]}
        if kind == "sql":
            t = q.lower()
            name = "count_in_transit" if "transit" in t or "on the way" in t else (
                "sum_totals" if ("total" in t or "spent" in t) and "how many" not in t else "count_orders")
            return {"source": "sql", "answer": self.sql.run(name, owner), "query": name}
        hits = self.kb.search(q, k=4)
        if self.use_reranker:
            hits = rerank(q, hits)
        return {"source": "vector", "hits": hits[:2]}
