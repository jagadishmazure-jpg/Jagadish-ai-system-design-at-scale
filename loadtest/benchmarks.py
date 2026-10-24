"""Micro-benchmarks and offline evals whose numbers the docs quote.

python -m loadtest.benchmarks            -> results/benchmarks.json
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import time

from sysdesign import backend, capacity
from sysdesign.context import measure
from sysdesign.distributed import batching_benchmark, prefix_cache_hit_rate
from sysdesign.knowledge import rerank
from sysdesign.rag import POLICY_DOCS, KnowledgeBase, tokens

ROOT = pathlib.Path(__file__).resolve().parent.parent

# labelled retrieval set: question -> the chunk that answers it (written for this repo)
RETRIEVAL_SET = [
    ("Do you ship internationally?", "shipping-intl"),
    ("how many countries do you ship to", "shipping-intl"),
    ("what is the warranty on electronics", "warranty"),
    ("does the warranty cover accidental damage", "warranty"),
    ("can I change my delivery address", "account-help"),
    ("how do I reset my password", "account-help"),
    ("do you price match", "price-match"),
    ("can support give me a discount code", "price-match"),
    ("how long until my refund shows up", "refund-timing"),
    ("when will my card refund appear", "refund-timing"),
    ("my item arrived damaged what do I do", "returns-damaged"),
    ("can I return a final sale item", "returns-window"),
    ("how many days do I have to return something", "returns-window"),
    ("how fast is express shipping", "shipping-speed"),
    ("how long is shipping", "shipping-speed"),
    ("arrived broken", "returns-damaged"),
]


def retrieval_eval(use_reranker: bool, k: int = 4) -> dict:
    kb = KnowledgeBase()
    hit1 = hitk = 0
    lat = []
    for q, gold in RETRIEVAL_SET:
        t0 = time.perf_counter()
        hits = kb.search(q, k=k, min_score=0.0)
        if use_reranker:
            hits = rerank(q, hits)
        lat.append(time.perf_counter() - t0)
        ids = [h["id"] for h in hits]
        hit1 += bool(ids) and ids[0] == gold
        hitk += gold in ids[:2]
    n = len(RETRIEVAL_SET)
    lat.sort()
    return {"reranker": use_reranker, "hit_at_1": round(hit1 / n, 4), "hit_at_2": round(hitk / n, 4),
            "p50_ms": round(lat[n // 2] * 1000, 3)}


def reranker_decision() -> dict:
    off, on = retrieval_eval(False), retrieval_eval(True)
    gain = on["hit_at_1"] - off["hit_at_1"]
    added = on["p50_ms"] - off["p50_ms"]
    # rule (docs/requirements.md): keep the reranker only if it buys >= 5 points of hit@1 for
    # <= 10 ms of added p50, since retrieval's sub-budget is 50 ms
    keep = gain >= 0.05 and added <= 10
    return {"off": off, "on": on, "hit_at_1_gain": round(gain, 4), "added_p50_ms": round(added, 3),
            "rule": "keep if hit@1 gain >= 0.05 and added p50 <= 10 ms", "keep_reranker": keep}


def chunk_size_eval(sizes=(8, 16, 32, 64)) -> list[dict]:
    """Re-chunk the policy handbook (all docs joined) at several sizes and check whether the top hit
    contains the key fact for each question, plus how many tokens a top-2 retrieval returns."""
    facts = {"shipping-intl": "40", "warranty": "12", "refund-timing": "5", "returns-window": "30",
             "returns-damaged": "14", "shipping-speed": "express", "price-match": "price", "account-help": "address"}
    words = []
    for _cid, text in POLICY_DOCS:
        words += text.split()
    out = []
    for size in sizes:
        chunks = [(f"c{i}", " ".join(words[i:i + size])) for i in range(0, len(words), max(1, size // 2))]
        kb = KnowledgeBase(chunks)
        ok, toks = 0, 0
        for q, gold in RETRIEVAL_SET:
            hits = kb.search(q, k=2, min_score=0.0)
            toks += sum(len(tokens(h["text"])) for h in hits)
            ok += any(facts[gold].lower() in h["text"].lower() for h in hits[:1])
        out.append({"chunk_words": size, "overlap_words": size // 2, "chunks": len(chunks),
                    "top1_has_fact": round(ok / len(RETRIEVAL_SET), 4),
                    "avg_tokens_returned": round(toks / len(RETRIEVAL_SET), 1)})
    return out


def main() -> dict:
    res = {"note": "measured on one machine; fake model latency where a model is involved"}
    res["batching"] = [asyncio.run(batching_benchmark(b)) for b in (1, 4, 16)]
    res["sticky_routing"] = {"round_robin": prefix_cache_hit_rate("round_robin"), "chwbl": prefix_cache_hit_rate("chwbl")}
    res["index_proof"] = backend.index_proof()
    res["context_funnel"] = measure()
    res["capacity_1x"] = capacity.report()
    res["capacity_10x"] = capacity.report(users=100_000_000)
    res["routing_mix_solution"] = capacity.solve_mix(2_000, 1_000, 4, 0.01)
    res["reranker"] = reranker_decision()
    res["chunk_size"] = chunk_size_eval()
    return res


if __name__ == "__main__":
    r = main()
    (ROOT / "results").mkdir(exist_ok=True)
    (ROOT / "results" / "benchmarks.json").write_text(json.dumps(r, indent=1) + "\n")
    print(json.dumps({k: r[k] for k in ("batching", "reranker", "chunk_size", "index_proof")}, indent=1))
