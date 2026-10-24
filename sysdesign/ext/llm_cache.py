"""Talk 15: caching for LLM apps (part 1).

Story (fictional): Kestrel is a support assistant for merchants on the made-up
Tidewell storefront platform. Its model bill grew much faster than its traffic.
Everything here is a simulation with illustrative prices; nothing calls a real
provider.

Pieces:
- expected_cost / cost_curve / curve_svg: blended cost per request at hit rate H.
- PrefixCacheSim: a provider-style prompt (prefix) cache. 1,024-token minimum,
  128-token blocks, 5-minute TTL that is renewed on every hit, cached input
  billed at 10% of the normal input rate.
- build_prompt: the same prompt laid out timestamp-first or stable-first.
- replay_incident: June vs August traffic, wrong hypotheses, retry spikes, fix.
- multi_turn: append-only history vs rewritten history vs summarise-at-limit.
- EmbeddingCache: write-through, keyed on a hash of the chunk text.
- ResponseCache lives in sysdesign/cache.py next to SemanticCache.
"""
from __future__ import annotations

import hashlib
import math
import random
import statistics
from dataclasses import dataclass, field

# ---------------------------------------------------------------- prices
# Illustrative list prices (USD per 1M tokens), as of 2026-10. Not a real quote.
PRICE_IN_PER_M = 2.50
PRICE_OUT_PER_M = 10.00
CACHED_DISCOUNT = 0.10  # cached input billed at 10% of the input rate

MIN_CACHE_TOKENS = 1024
CACHE_BLOCK = 128
CACHE_TTL_S = 300.0

# Simulated prefill model (assumption, not a measurement of any provider):
# fixed overhead plus time per uncached token; cached tokens are almost free.
TTFT_BASE_S = 0.25
TTFT_PER_1K_UNCACHED_S = 0.09
TTFT_PER_1K_CACHED_S = 0.005


def _check_nonneg(**kw) -> None:
    for k, v in kw.items():
        if v is None or (isinstance(v, float) and math.isnan(v)) or v < 0:
            raise ValueError(f"{k} must be a non-negative number")


def expected_cost(hit_rate: float, cost_hit: float, cost_miss: float) -> float:
    """E[cost] = H*C_hit + (1-H)*C_miss."""
    _check_nonneg(cost_hit=cost_hit, cost_miss=cost_miss)
    if hit_rate is None or isinstance(hit_rate, bool) or not (0.0 <= hit_rate <= 1.0):
        raise ValueError("hit_rate must be within 0..1")
    return hit_rate * cost_hit + (1.0 - hit_rate) * cost_miss


def cost_curve(cost_hit: float, cost_miss: float, steps: int = 10) -> list[tuple[float, float]]:
    if steps < 1:
        raise ValueError("steps must be >= 1")
    return [(round(i / steps, 4), expected_cost(i / steps, cost_hit, cost_miss)) for i in range(steps + 1)]


def curve_svg(points: list[tuple[float, float]], title: str = "Expected cost per request vs hit rate") -> str:
    """Tiny dependency-free SVG line chart."""
    w, h, pad = 520, 300, 50
    ymax = max(c for _, c in points) or 1.0
    xy = [(pad + x * (w - 2 * pad), h - pad - (c / ymax) * (h - 2 * pad)) for x, c in points]
    path = " ".join(f"{'M' if i == 0 else 'L'}{px:.1f},{py:.1f}" for i, (px, py) in enumerate(xy))
    ticks = "".join(
        f'<text x="{pad + t * (w - 2 * pad):.0f}" y="{h - pad + 18}" font-size="11" text-anchor="middle">'
        f"{int(t * 100)}%</text>" for t in (0, 0.25, 0.5, 0.75, 1.0))
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" font-family="sans-serif">'
            f'<rect width="{w}" height="{h}" fill="white"/>'
            f'<text x="{w / 2}" y="22" font-size="14" text-anchor="middle">{title}</text>'
            f'<line x1="{pad}" y1="{h - pad}" x2="{w - pad}" y2="{h - pad}" stroke="black"/>'
            f'<line x1="{pad}" y1="{pad}" x2="{pad}" y2="{h - pad}" stroke="black"/>{ticks}'
            f'<text x="{w / 2}" y="{h - 8}" font-size="11" text-anchor="middle">cache hit rate</text>'
            f'<text x="12" y="{pad - 8}" font-size="11">${ymax:.4f}</text>'
            f'<path d="{path}" fill="none" stroke="#1f6feb" stroke-width="2"/></svg>\n')


# ---------------------------------------------------------------- prompt cache
Segment = tuple[str, int]  # (content id, token count)


@dataclass
class CallResult:
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    cost_usd: float
    ttft_s: float


def call_cost(input_tokens: int, cached_tokens: int, output_tokens: int) -> float:
    _check_nonneg(input_tokens=input_tokens, cached_tokens=cached_tokens, output_tokens=output_tokens)
    if cached_tokens > input_tokens:
        raise ValueError("cached_tokens cannot exceed input_tokens")
    unc = input_tokens - cached_tokens
    return (unc * PRICE_IN_PER_M + cached_tokens * PRICE_IN_PER_M * CACHED_DISCOUNT
            + output_tokens * PRICE_OUT_PER_M) / 1e6


def simulated_ttft(input_tokens: int, cached_tokens: int) -> float:
    unc = input_tokens - cached_tokens
    return TTFT_BASE_S + unc / 1000 * TTFT_PER_1K_UNCACHED_S + cached_tokens / 1000 * TTFT_PER_1K_CACHED_S


def billable_cached(prefix_tokens: int) -> int:
    if prefix_tokens < MIN_CACHE_TOKENS:
        return 0
    return prefix_tokens // CACHE_BLOCK * CACHE_BLOCK


class PrefixCacheSim:
    """Caches every segment boundary of each prompt it processes.

    A token's KV state depends on every token before it, so a stored prefix is
    reusable only when the new prompt starts with exactly the same segments.
    Granularity is per segment (real providers match per token block); the
    billable amount is still rounded down to 128-token blocks above 1,024.
    """

    def __init__(self, ttl_s: float = CACHE_TTL_S) -> None:
        self.ttl_s = ttl_s
        self.expiry: dict[str, float] = {}

    @staticmethod
    def _keys(segments: list[Segment]) -> list[tuple[str, int]]:
        out, h, n = [], hashlib.sha256(), 0
        for sid, toks in segments:
            if toks < 0:
                raise ValueError("segment tokens must be >= 0")
            h.update(sid.encode() + b"\x00")
            n += toks
            out.append((h.copy().hexdigest()[:20], n))
        return out

    def call(self, segments: list[Segment], now: float, output_tokens: int = 0) -> CallResult:
        keys = self._keys(segments)
        matched_idx = -1
        for i, (k, _) in enumerate(keys):
            exp = self.expiry.get(k)
            if exp is not None and exp >= now:
                matched_idx = i
            else:
                break
        prefix = keys[matched_idx][1] if matched_idx >= 0 else 0
        cached = billable_cached(prefix)
        for k, _ in keys:  # write all boundaries; hits renew their TTL
            self.expiry[k] = now + self.ttl_s
        total = keys[-1][1] if keys else 0
        return CallResult(total, cached, output_tokens, call_cost(total, cached, output_tokens),
                          simulated_ttft(total, cached))


# ---------------------------------------------------------------- prompt builder
@dataclass
class PromptParts:
    timestamp: str
    merchant: str
    system: Segment
    docs: list[Segment]
    history: list[Segment]
    question: Segment
    ts_tokens: int = 15
    merchant_tokens: int = 10


def build_prompt(p: PromptParts, layout: str) -> list[Segment]:
    """timestamp_first: what Kestrel shipped. stable_first: least to most volatile."""
    ts = (f"ts:{p.timestamp}", p.ts_tokens)
    mer = (f"merchant:{p.merchant}", p.merchant_tokens)
    if layout == "timestamp_first":
        return [ts, mer, p.system, *p.docs, *p.history, p.question]
    if layout == "stable_first":
        return [p.system, mer, *p.docs, *p.history, p.question, ts]
    raise ValueError("layout must be timestamp_first or stable_first")


# ---------------------------------------------------------------- incident replay
@dataclass
class Era:
    name: str
    turns_per_day: int
    system_tokens: int
    doc_tokens: int
    docs_per_turn: int
    history_per_turn: int
    question_tokens: int = 50
    output_tokens: int = 380
    price_in: float = PRICE_IN_PER_M
    price_out: float = PRICE_OUT_PER_M


JUNE = Era("june", 153_846, system_tokens=900, doc_tokens=300, docs_per_turn=2, history_per_turn=90)
AUGUST = Era("august", 200_000, system_tokens=4200, doc_tokens=600, docs_per_turn=3, history_per_turn=330)


def _pct(xs: list[float], q: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, int(q * len(s)))]


def replay(era: Era, layout: str, window_s: int = 3600, retry_rate: float = 0.03, merchants: int = 50,
           seed: int = 15) -> dict:
    """Replay one window of traffic at the era's daily rate and scale cost to a 30-day month."""
    rng = random.Random(seed)
    n = round(era.turns_per_day * window_s / 86400)
    cache = PrefixCacheSim()
    weights = [1 / (i + 1) for i in range(merchants)]
    calls, minute_hits, sec_hits, seen = [], {}, {}, set()
    q_tokens, conv_turn = [], {}
    arrivals = sorted(rng.uniform(0, window_s) for _ in range(n))
    for i, t in enumerate(arrivals):
        m = rng.choices(range(merchants), weights)[0]
        conv = rng.randrange(n // 4 + 1)
        k = conv_turn.get(conv, 0) % 6
        conv_turn[conv] = k + 1
        parts = PromptParts(
            timestamp=str(int(t)), merchant=f"m{m}", system=("system:v2" if era is AUGUST else "system:v1",
                                                            era.system_tokens),
            docs=[(f"doc:{m}:{rng.randrange(40)}", era.doc_tokens) for _ in range(era.docs_per_turn)],
            history=[(f"hist:{conv}:{j}", era.history_per_turn) for j in range(k)],
            question=(f"q:{i}", era.question_tokens))
        seg = build_prompt(parts, layout)
        q_tokens.append(era.question_tokens)
        attempts = 2 if rng.random() < retry_rate else 1
        for a in range(attempts):  # a retry lands in the same second with the identical prompt
            r = cache.call(seg, t + 0.4 * a, era.output_tokens)
            cost = ((r.input_tokens - r.cached_tokens) * era.price_in
                    + r.cached_tokens * era.price_in * CACHED_DISCOUNT + r.output_tokens * era.price_out) / 1e6
            same_sec = (int(t), m) in seen
            seen.add((int(t), m))
            calls.append((r, cost, a > 0, same_sec))
            sh = sec_hits.setdefault(int(t), [0, 0])
            sh[0] += r.cached_tokens > 0
            sh[1] += 1
            mh = minute_hits.setdefault(int(t // 60), [0, 0])
            mh[0] += r.cached_tokens > 0
            mh[1] += 1
    tin = sum(c[0].input_tokens for c in calls)
    tc = sum(c[0].cached_tokens for c in calls)
    hits = [c for c in calls if c[0].cached_tokens > 0]
    cost = sum(c[1] for c in calls)
    in_cost = sum(c[1] - c[0].output_tokens * era.price_out / 1e6 for c in calls)
    per_min = [h / tot for h, tot in minute_hits.values()]
    ttfts = [c[0].ttft_s for c in calls if not c[2]]
    scale = 86400 / window_s
    return {
        "era": era.name, "layout": layout, "turns_simulated": n, "calls": len(calls),
        "retries": sum(c[2] for c in calls), "avg_input_tokens": round(tin / len(calls), 1),
        "avg_question_tokens": statistics.mean(q_tokens),
        "request_hit_rate": round(len(hits) / len(calls), 4), "token_hit_rate": round(tc / tin, 4),
        "hits_from_retries": sum(c[2] for c in hits),
        "hits_from_same_second_same_merchant": sum(c[3] for c in hits),
        "seconds_with_traffic": len(sec_hits),
        "seconds_with_zero_hits": sum(1 for h, _ in sec_hits.values() if h == 0),
        "per_minute_hit_rate_mean": round(statistics.mean(per_min), 4),
        "per_minute_hit_rate_max": round(max(per_min), 4),
        "per_minute_hit_rate_zero_minutes": sum(1 for x in per_min if x == 0),
        "input_cost_per_day_usd": round(in_cost * scale, 2), "cost_per_day_usd": round(cost * scale, 2),
        "monthly_bill_usd": round(cost * scale * 30, 0),
        "ttft_p50_s": round(_pct(ttfts, 0.5), 3), "ttft_p95_s": round(_pct(ttfts, 0.95), 3),
    }


def replay_incident(window_s: int = 3600) -> dict:
    june = replay(JUNE, "timestamp_first", window_s)
    aug = replay(AUGUST, "timestamp_first", window_s)
    fixed = replay(AUGUST, "stable_first", window_s)
    traffic = AUGUST.turns_per_day / JUNE.turns_per_day
    bill = aug["cost_per_day_usd"] / june["cost_per_day_usd"]
    hypotheses = {
        "users_send_longer_messages": {
            "june_avg_question_tokens": june["avg_question_tokens"],
            "august_avg_question_tokens": aug["avg_question_tokens"],
            "ruled_out": aug["avg_question_tokens"] <= june["avg_question_tokens"] * 1.05},
        "provider_raised_prices": {
            "june_prices": [JUNE.price_in, JUNE.price_out], "august_prices": [AUGUST.price_in, AUGUST.price_out],
            "ruled_out": (JUNE.price_in, JUNE.price_out) == (AUGUST.price_in, AUGUST.price_out)},
    }
    return {
        "traffic_growth": round(traffic, 2), "bill_growth": round(bill, 2),
        "prompt_growth": round(aug["avg_input_tokens"] / june["avg_input_tokens"], 2),
        "hypotheses": hypotheses, "june": june, "august_timestamp_first": aug, "august_stable_first": fixed,
        "fix": {
            "input_cost_reduction": round(1 - fixed["input_cost_per_day_usd"] / aug["input_cost_per_day_usd"], 4),
            "monthly_bill_before": aug["monthly_bill_usd"], "monthly_bill_after": fixed["monthly_bill_usd"],
            "ttft_p50_before": aug["ttft_p50_s"], "ttft_p50_after": fixed["ttft_p50_s"]},
    }


# ---------------------------------------------------------------- multi-turn
def multi_turn(mode: str, turns: int = 12, gap_s: float = 30.0, system_tokens: int = 4200,
               turn_tokens: int = 400, context_limit: int = 8000, summary_tokens: int = 600) -> dict:
    """append_only | rewrite | summarise. Returns cached share of input across the conversation."""
    if mode not in ("append_only", "rewrite", "summarise"):
        raise ValueError("bad mode")
    cache = PrefixCacheSim()
    history: list[Segment] = []
    tin = tc = 0
    per_turn, summaries = [], 0
    for t in range(turns):
        if mode == "rewrite":  # history re-rendered every turn (e.g. 'N turns ago' labels)
            history = [(f"h{j}@{t}", turn_tokens) for j in range(t)]
        seg = [("system", system_tokens), *history, (f"user{t}", 60)]
        if mode == "summarise" and sum(s[1] for s in seg) > context_limit:
            history = [(f"summary@{t}", summary_tokens)]
            summaries += 1
            seg = [("system", system_tokens), *history, (f"user{t}", 60)]
        r = cache.call(seg, t * gap_s)
        tin += r.input_tokens
        tc += r.cached_tokens
        per_turn.append(r.cached_tokens)
        if mode != "rewrite":
            history = [*history, (f"user{t}", 60), (f"reply{t}", turn_tokens - 60)]
    return {"mode": mode, "input_tokens": tin, "cached_tokens": tc, "cached_share": round(tc / tin, 4),
            "summaries": summaries, "cached_per_turn": per_turn}


# ---------------------------------------------------------------- embedding cache (write-through)
def text_key(text: str, model: str = "embed-v1") -> str:
    return hashlib.sha256(f"{model}\x00{text}".encode()).hexdigest()


@dataclass
class EmbeddingCache:
    """Write-through: the indexer writes every chunk through this cache, so the
    cache always holds the vector for the current text. Content-hash keys mean a
    changed chunk simply gets a new key; an old vector can never be served for
    new text."""
    embed_fn: object
    model: str = "embed-v1"
    store: dict = field(default_factory=dict)
    stats: dict = field(default_factory=lambda: {"hits": 0, "misses": 0, "embed_calls": 0})

    def index(self, chunks: list[str]) -> list:
        out = []
        for c in chunks:
            k = text_key(c, self.model)
            if k in self.store:
                self.stats["hits"] += 1
            else:
                self.stats["misses"] += 1
                self.stats["embed_calls"] += 1
                self.store[k] = self.embed_fn(c)
            out.append(self.store[k])
        return out

    def lookup(self, text: str):
        return self.store.get(text_key(text, self.model))

    def hit_rate(self) -> float:
        tot = self.stats["hits"] + self.stats["misses"]
        return self.stats["hits"] / tot if tot else 0.0


# ---------------------------------------------------------------- chat benchmark
_FAQ = [
    "what is your refund policy", "how long does shipping take", "do you ship internationally",
    "how do i change my delivery address", "can i return a sale item", "what payment methods do you accept",
    "how do i cancel my order", "do you offer gift wrapping", "how do i reset my password",
    "what is the warranty on electronics", "how do exchanges work", "when will i get my refund",
]
_REWORDS = {
    "what is your refund policy": "can you explain the refund policy",
    "how long does shipping take": "how many days is shipping",
    "do you ship internationally": "can you ship to other countries",
    "how do i cancel my order": "i want to cancel an order how",
}


def _surface_variant(rng: random.Random, q: str) -> str:
    v = q
    if rng.random() < 0.5:
        v = v.capitalize()
    if rng.random() < 0.5:
        v += rng.choice(["?", "??", " ?", "."])
    if rng.random() < 0.3:
        v = v.replace(" ", "  ", 1)
    return v


def chat_stream(n: int = 3000, tenants: int = 3, seed: int = 7) -> list[tuple[str, str, str]]:
    """(tenant, intent, text). 45% policy FAQ (surface variants and some rewordings),
    55% order-status questions carrying a unique order id."""
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        t = f"tenant-{rng.randrange(tenants)}"
        if rng.random() < 0.45:
            q = rng.choice(_FAQ)
            if q in _REWORDS and rng.random() < 0.25:
                q = _REWORDS[q]
            out.append((t, "policy", _surface_variant(rng, q)))
        else:
            out.append((t, "order_status", f"where is order #A-{rng.randrange(10000, 99999)}?"))
    return out


def chat_benchmark(redis_factory, n: int = 3000) -> dict:
    """Exact-match ResponseCache alone vs exact first then SemanticCache on a miss."""
    from sysdesign.cache import ResponseCache, SemanticCache

    stream = chat_stream(n)
    rc = ResponseCache(redis_factory())
    exact_hits = 0
    for t, intent, q in stream:
        _, hit = rc.get_or_compute(t, intent, q, lambda q=q: f"answer to {q.lower()}")
        exact_hits += hit
    rc2, sc = ResponseCache(redis_factory()), SemanticCache(redis_factory(), threshold=0.85, ttl_s=86400)
    layered, sem_hits = 0, 0
    for t, intent, q in stream:
        _, hit = rc2.get_or_compute(t, intent, q, lambda: "")
        if hit:
            layered += 1
            continue
        if intent == "policy":
            if sc.get(t, {"chat"}, intent, q) is not None:
                layered += 1
                sem_hits += 1
            else:
                sc.put(t, {"chat"}, intent, q, f"answer to {q.lower()}", ["policy#1"])
    policy = sum(1 for s in stream if s[1] == "policy")
    return {"requests": n, "policy_requests": policy, "exact_hit_rate": round(exact_hits / n, 4),
            "exact_hit_rate_policy_only": round(exact_hits / policy, 4),
            "exact_plus_semantic_hit_rate": round(layered / n, 4), "semantic_extra_hits": sem_hits,
            "order_status_cached": 0 if rc.stats["refused"] >= n - policy else -1}


def reindex_benchmark(docs: int = 200, chunks_per_doc: int = 5, changed_docs: int = 20) -> dict:
    from sysdesign.knowledge import embed

    corpus = {d: [f"doc {d} paragraph {p} text v1" for p in range(chunks_per_doc)] for d in range(docs)}
    ec = EmbeddingCache(embed)
    ec.index([c for cs in corpus.values() for c in cs])
    first = dict(ec.stats)
    for d in range(changed_docs):  # each changed doc edits two paragraphs
        corpus[d][0] = corpus[d][0].replace("v1", "v2")
        corpus[d][1] = corpus[d][1].replace("v1", "v2")
    ec.stats = {"hits": 0, "misses": 0, "embed_calls": 0}
    ec.index([c for cs in corpus.values() for c in cs])
    return {"chunks": docs * chunks_per_doc, "first_index_embed_calls": first["embed_calls"],
            "changed_docs": changed_docs, "reindex_embed_calls": ec.stats["embed_calls"],
            "reindex_hit_rate": round(ec.hit_rate(), 4)}


def run_all(window_s: int = 3600) -> dict:
    import fakeredis

    curve = cost_curve(call_cost(6900, 4096, 380), call_cost(6900, 0, 380))
    return {
        "note": "Simulated. Illustrative prices; simulated prefill model; one-hour window scaled to a day.",
        "cost_curve": [{"hit_rate": h, "cost_usd": round(c, 6)} for h, c in curve],
        "incident": replay_incident(window_s),
        "multi_turn": {m: {k: v for k, v in multi_turn(m).items() if k != "cached_per_turn"}
                       for m in ("append_only", "rewrite", "summarise")},
        "response_cache": chat_benchmark(fakeredis.FakeRedis),
        "embedding_reindex": reindex_benchmark(),
    }
