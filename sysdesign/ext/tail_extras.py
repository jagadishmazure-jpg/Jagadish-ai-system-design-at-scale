"""Talk 17 add-ons to talk 16 (tail latency): the mean hides the tail, 1% of requests is not 1% of
users, and a page that renders what is ready instead of waiting for its slowest part.

Simulated: latencies are drawn from a seeded lognormal (median 55 ms, sigma 0.99), chosen so the
shape resembles the talk's example. The talk's own figures are kept in QUOTED and never mixed in.
"""
from __future__ import annotations

import math
import random
import statistics

QUOTED = {"mean_ms": 90, "p50_ms": 55, "p99_ms": 543, "source": "quoted from the talk; not measured here"}
MEDIAN_MS = 55.0
SIGMA = 0.99


def pct(xs: list[float], p: float) -> float:
    s = sorted(xs)
    i = min(len(s) - 1, max(0, math.ceil(p / 100 * len(s)) - 1))
    return s[i]


def sample(n: int = 100_000, seed: int = 7, median_ms: float = MEDIAN_MS, sigma: float = SIGMA) -> list[float]:
    rng = random.Random(seed)
    mu = math.log(median_ms)
    return [rng.lognormvariate(mu, sigma) for _ in range(n)]


def summary(xs: list[float]) -> dict:
    mean = statistics.fmean(xs)
    return {"n": len(xs), "mean_ms": round(mean, 1), "p50_ms": round(pct(xs, 50), 1),
            "p90_ms": round(pct(xs, 90), 1), "p99_ms": round(pct(xs, 99), 1),
            "share_slower_than_mean": round(sum(x > mean for x in xs) / len(xs), 4)}


def users_hit(p_slow: float, n_requests: int) -> float:
    """Chance a user making n requests sees at least one slow one."""
    if not 0 <= p_slow <= 1 or n_requests < 0:
        raise ValueError("p_slow in [0,1] and n_requests >= 0")
    return 1 - (1 - p_slow) ** n_requests


def users_hit_sim(xs: list[float], threshold_ms: float, n_requests: int, users: int = 4000, seed: int = 3) -> float:
    rng = random.Random(seed)
    hit = 0
    for _ in range(users):
        if any(rng.choice(xs) > threshold_ms for _ in range(n_requests)):
            hit += 1
    return round(hit / users, 4)


# ------------------------------------------------------------------ partial page
def render_page(part_ms: list[float], deadline_ms: float | None) -> dict:
    """Wait-for-all when deadline is None; otherwise show what is ready at the deadline and mark the
    rest 'loading' (they fill in later, the page is usable now)."""
    slowest = max(part_ms)
    if deadline_ms is None or slowest <= deadline_ms:
        return {"ready_ms": slowest, "loading": 0, "parts": ["ready"] * len(part_ms)}
    parts = ["ready" if t <= deadline_ms else "loading" for t in part_ms]
    return {"ready_ms": deadline_ms, "loading": parts.count("loading"), "parts": parts}


def page_study(fanout: int = 100, pages: int = 2000, deadline_ms: float = 250.0, seed: int = 11) -> dict:
    rng = random.Random(seed)
    mu = math.log(MEDIAN_MS)
    full, partial, loading = [], [], []
    for _ in range(pages):
        parts = [rng.lognormvariate(mu, SIGMA) for _ in range(fanout)]
        full.append(render_page(parts, None)["ready_ms"])
        r = render_page(parts, deadline_ms)
        partial.append(r["ready_ms"])
        loading.append(r["loading"])
    return {"fanout": fanout, "pages": pages, "deadline_ms": deadline_ms,
            "wait_for_all": {"p50_ms": round(pct(full, 50), 1), "p99_ms": round(pct(full, 99), 1)},
            "partial": {"p50_ms": round(pct(partial, 50), 1), "p99_ms": round(pct(partial, 99), 1)},
            "parts_loading_mean": round(statistics.fmean(loading), 2),
            "parts_loading_p99": pct(loading, 99),
            "pages_with_any_loading": round(sum(x > 0 for x in loading) / pages, 4)}


def results() -> dict:
    xs = sample()
    s = summary(xs)
    thr = pct(xs, 99)
    return {"note": "simulated lognormal latencies, seeded", "distribution": s, "quoted": QUOTED,
            "users_hit_p99": [{"requests_per_session": n, "formula": round(users_hit(0.01, n), 4),
                               "simulated": users_hit_sim(xs, thr, n)} for n in (1, 10, 50, 100)],
            "page": page_study()}
