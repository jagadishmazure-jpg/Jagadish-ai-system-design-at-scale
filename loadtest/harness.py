"""Deterministic load model: the real components (admission, router, cache, guardrails, model gateway,
circuit breaker, queue, autoscaler) driven by a seeded discrete-event simulation in virtual time.

What is real: every admit/deny, cache hit, route, retry, breaker transition,
fallback, queue claim and scaling decision is made by the repo's code.
What is simulated: arrival times, provider latency/outages (FakeProvider),
replica slot scheduling, and the clock. Scale is small (thousands of
requests, a few virtual minutes) so it runs in CI in seconds. These are
model results, not a benchmark of real hardware.

Usage: python -m loadtest.harness            # writes loadtest/results/results.json
       python -m loadtest.harness --check    # fails if committed results differ from a fresh run
"""
from __future__ import annotations

import heapq
import itertools
import json
import math
import pathlib
import random
import sys

import fakeredis

from sysdesign.admission import Admission, Limits
from sysdesign.agent import SupportAgent
from sysdesign.auth import Identity
from sysdesign.autoscaler import Autoscaler
from sysdesign.cache import SemanticCache
from sysdesign.clock import FakeClock
from sysdesign.jobqueue import JobQueue
from sysdesign.observability import Metrics, percentile
from sysdesign.providers import FakeProvider
from sysdesign.resilience import CircuitBreaker, ModelGateway, RetryPolicy

OUT = pathlib.Path(__file__).resolve().parent / "results" / "results.json"
CLIENT_TIMEOUT_S = 10.0
OVERHEAD_S = {"rules": 0.004, "cache": 0.012, "gateway": 0.002, "guardrail": 0.003, "human": 0.004,
              "classifier": 0.006, "small_model": 0.02, "large_model": 0.02}

POLICY_QS = [
    ["what is the return window for electronics", "how many days do i have to return electronics",
     "electronics return window days"],
    ["can i pay cash on delivery", "is cash on delivery available", "pay with cash on delivery for my order"],
    ["how long does standard shipping take", "standard shipping how many business days",
     "how long standard shipping"],
    ["do gift cards expire", "can gift cards be exchanged for cash", "gift card expire"],
    ["how fast is express shipping", "express shipping how many days", "express shipping fee"],
    ["when will my refund reach my card after return", "how long for refunds to the original payment method",
     "refund to original payment method days"],
]
OTHER = ["hi", "thanks", "where is my order KG-100001", "price of SKU-KETTLE",
         "my split shipment arrived with a missing item and also one damaged box from two orders",
         "i want a refund for order KG-100002"]


def r3(x: float) -> float:
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(x, 3)


def summarize(lat: list[float], n: int, errors: int, rejected: int, cost: float, hits: int = 0,
              cache_eligible: int = 0) -> dict:
    return {"requests": n, "p50_s": r3(percentile(lat, 50)), "p95_s": r3(percentile(lat, 95)),
            "p99_s": r3(percentile(lat, 99)), "error_rate": r3(errors / n if n else 0.0),
            "rate_429": r3(rejected / n if n else 0.0), "cost_usd": round(cost, 6),
            "cost_per_1k_requests_usd": round(1000 * cost / n, 6) if n else 0.0,
            "cache_hit_rate": r3(hits / cache_eligible) if cache_eligible else 0.0}


class Cluster:
    """Replicas x slots; requests take the earliest free slot on the chosen replica."""

    def __init__(self, replicas: int, slots: int) -> None:
        self.free = {r: [0.0] * slots for r in range(replicas)}
        self.alive = {r: True for r in range(replicas)}
        self._rr = itertools.cycle(range(replicas))

    def pick(self, now: float, health_checked: bool, killed_at: dict) -> int:
        for _ in range(len(self.free)):
            r = next(self._rr)
            dead = r in killed_at and now >= killed_at[r]
            detected = dead and now >= killed_at[r] + 2.0  # health check interval + threshold
            if health_checked and detected:
                continue
            return r
        return 0

    def schedule(self, r: int, arrival: float) -> float:
        slot_free = heapq.heappop(self.free[r])
        start = max(arrival, slot_free)
        return start

    def finish(self, r: int, end: float) -> None:
        heapq.heappush(self.free[r], end)


def make_agent(clock, *, cache=True, ladder=True, providers=None, breaker=True, fallback=True, retries=True,
               limits=None, slots=64):
    r = fakeredis.FakeRedis()
    lim = limits or {"standard": Limits(rate_per_s=2.0, burst=10, tokens_per_day=10**9, tokens_per_month=10**10,
                                        max_concurrent_model_calls=slots)}
    adm = Admission(r, clock=clock, limits=lim)
    providers = providers or [FakeProvider("primary"), FakeProvider("fallback", seed=9,
                                                                    latency_s={"small": 0.35, "large": 1.6})]
    breakers = {p.name: CircuitBreaker(failure_threshold=5, window_s=10, cooldown_s=5, clock=clock)
                for p in providers}
    gw = ModelGateway(providers, breakers=breakers, retry=RetryPolicy(max_retries=2, base_s=0.2, cap_s=2.0),
                      clock=clock, retries_enabled=retries, breaker_enabled=breaker, fallback_enabled=fallback)
    agent = SupportAgent(adm, SemanticCache(r, threshold=0.8, ttl_s=3600, clock=clock), gw, metrics=Metrics(),
                         clock=clock, cache_enabled=cache, ladder_enabled=ladder, rate_check=False)
    return agent, adm


def run_traffic(agent, adm, clock, arrivals, *, rate_limit=True, replicas=3, slots=8, health_checked=True,
                killed_at=None):
    """arrivals: list of (t, user, text). Returns per-request records."""
    killed_at = killed_at or {}
    cluster = Cluster(replicas, slots)
    recs = []
    for t, user, text in arrivals:
        ident = Identity(user, "retail", "standard", frozenset({"chat"}))
        clock.t = t
        if rate_limit:
            d = adm.check_rate("retail", user, "standard")
            if not d.allowed:
                recs.append({"t": t, "user": user, "status": 429, "lat": OVERHEAD_S["gateway"], "cost": 0.0,
                             "cached": False, "intent": "rate_limited"})
                continue
        rep = cluster.pick(t, health_checked, killed_at)
        if rep in killed_at and t >= killed_at[rep]:
            # connection to a dead replica fails after a connect timeout
            recs.append({"t": t, "user": user, "status": 502, "lat": 1.0, "cost": 0.0, "cached": False,
                         "intent": "dead_replica"})
            continue
        start = cluster.schedule(rep, t)
        if start - t > CLIENT_TIMEOUT_S:
            cluster.finish(rep, start)  # slot untouched; client gave up while waiting
            recs.append({"t": t, "user": user, "status": 504, "lat": CLIENT_TIMEOUT_S, "cost": 0.0,
                         "cached": False, "intent": "client_timeout"})
            continue
        clock.t = start
        res = agent.handle(ident, text)
        service = (clock.t - start) + OVERHEAD_S.get(res.rung, 0.01)
        end = start + service
        cluster.finish(rep, end)
        lat = end - t
        status = res.status if lat <= CLIENT_TIMEOUT_S else 504
        recs.append({"t": t, "user": user, "status": status, "lat": min(lat, CLIENT_TIMEOUT_S), "text": text,
                     "sources": res.sources,
                     "cost": res.cost_usd, "cached": res.cached, "intent": res.intent, "rung": res.rung})
    return recs


def stats(recs, users=None, cache_intent="policy"):
    rs = [r for r in recs if users is None or r["user"] in users]
    ok = [r["lat"] for r in rs if r["status"] == 200]
    errors = sum(1 for r in rs if r["status"] >= 500)
    rejected = sum(1 for r in rs if r["status"] == 429)
    eligible = [r for r in rs if r["intent"] == cache_intent and r["status"] == 200]
    hits = sum(1 for r in eligible if r["cached"])
    return summarize(ok, len(rs), errors, rejected, sum(r["cost"] for r in rs), hits, len(eligible))


def poisson(rng, rate, t0, t1):
    t = t0
    while True:
        t += rng.expovariate(rate)
        if t >= t1:
            return
        yield t


def mixed_text(rng):
    if rng.random() < 0.55:
        return rng.choice(rng.choice(POLICY_QS))
    return rng.choice(OTHER)


# ---------------------------------------------------------------- scenarios

def scenario_rate_limit():
    rng = random.Random(1)
    arrivals = [(t, f"u{rng.randrange(300)}", mixed_text(rng)) for t in poisson(rng, 12, 0, 60)]
    arrivals += [(t, "abuser", f"compare two orders with a split shipment and a missing item, variant {i}")
                 for i, t in enumerate(poisson(rng, 30, 5, 60))]
    arrivals.sort()
    normal = {a[1] for a in arrivals if a[1] != "abuser"}
    out = {}
    for mode, rl in (("off", False), ("on", True)):
        clock = FakeClock(0)
        agent, adm = make_agent(clock, cache=False)
        recs = run_traffic(agent, adm, clock, arrivals, rate_limit=rl, replicas=3, slots=4)
        out[mode] = {"normal_users": stats(recs, normal), "abuser": stats(recs, {"abuser"})}
    return out


def scenario_cache():
    rng = random.Random(2)
    arrivals = [(t, f"u{rng.randrange(2000)}", rng.choice(rng.choice(POLICY_QS))) for t in poisson(rng, 8, 0, 120)]
    out = {}
    for mode, c in (("off", False), ("on", True)):
        clock = FakeClock(0)
        agent, adm = make_agent(clock, cache=c)
        recs = run_traffic(agent, adm, clock, arrivals)
        s = stats(recs)
        # a hit is "wrong" if its cached sources differ from what fresh retrieval would cite
        hits = [r for r in recs if r["cached"]]
        wrong = sum(1 for r in hits if r["sources"] != sorted(c.chunk_id for c, _ in agent.index.search(r["text"])))
        s["cache_hits"] = len(hits)
        s["wrong_source_hits"] = wrong
        out[mode] = s
    return out


def scenario_router():
    rng = random.Random(3)
    arrivals = [(t, f"u{rng.randrange(2000)}", mixed_text(rng)) for t in poisson(rng, 8, 0, 120)]
    out = {}
    for mode, ladder in (("off_all_large_model", False), ("on_ladder", True)):
        clock = FakeClock(0)
        agent, adm = make_agent(clock, cache=False, ladder=ladder)
        recs = run_traffic(agent, adm, clock, arrivals)
        s = stats(recs)
        paths = {}
        for r in recs:
            p = paths.setdefault(r.get("rung", r["intent"]), {"requests": 0, "cost_usd": 0.0})
            p["requests"] += 1
            p["cost_usd"] += r["cost"]
        for p in paths.values():
            p["cost_per_request_usd"] = round(p["cost_usd"] / p["requests"], 8)
            p["cost_usd"] = round(p["cost_usd"], 6)
        s["paths"] = dict(sorted(paths.items()))
        out[mode] = s
    return out


def disruption_seconds(recs, t0, t1):
    bins = {}
    for r in recs:
        b = bins.setdefault(int(r["t"]), [0, 0])
        b[0] += 1
        b[1] += r["status"] >= 500
    return sum(1 for s in range(int(t0), int(t1)) if s in bins and bins[s][1] / bins[s][0] > 0.05)


def last_bad_second(recs):
    bad = [r["t"] for r in recs if r["status"] >= 500]
    return max(bad) if bad else None


def scenario_outage():
    rng = random.Random(4)
    policy = [(t, f"u{rng.randrange(5000)}", rng.choice(rng.choice(POLICY_QS)) + f" ref {i}")
              for i, t in enumerate(poisson(rng, 6, 0, 180))]
    out = {}
    for mode, (br, fb, rt) in {"off_naive_retries": (False, False, True), "on_breaker_fallback": (True, True, True)}.items():
        clock = FakeClock(0)
        primary = FakeProvider("primary")
        fallback = FakeProvider("fallback", seed=9, latency_s={"small": 0.35, "large": 1.6})
        agent, adm = make_agent(clock, cache=False, providers=[primary, fallback], breaker=br, fallback=fb,
                                retries=rt)
        recs = []
        for phase, (a, b) in (("before", (0, 60)), ("outage", (60, 120)), ("after", (120, 180))):
            primary.outage = phase == "outage"
            chunk = [x for x in policy if a <= x[0] < b]
            recs += run_traffic(agent, adm, clock, chunk, rate_limit=False)
        s = stats(recs)
        during = [r for r in recs if 60 <= r["t"] < 120]
        s["outage_window"] = stats(during)
        s["disrupted_seconds"] = disruption_seconds(recs, 0, 180)
        lb = last_bad_second(recs)
        s["recovery_s_after_outage_end"] = r3(max(0.0, lb - 120)) if lb is not None else 0.0
        s["primary_calls"] = primary.calls
        s["fallback_calls"] = fallback.calls
        s["breaker_transitions"] = len(agent.gateway.breakers["primary"].transitions)
        out[mode] = s
    return out


def scenario_replica_failure():
    rng = random.Random(5)
    arrivals = [(t, f"u{rng.randrange(3000)}", mixed_text(rng)) for t in poisson(rng, 10, 0, 90)]
    out = {}
    for mode, hc in (("off_no_health_checks", False), ("on_health_checked_lb", True)):
        clock = FakeClock(0)
        agent, adm = make_agent(clock)
        recs = run_traffic(agent, adm, clock, arrivals, rate_limit=False, health_checked=hc, killed_at={1: 30.0})
        out[mode] = stats(recs)
    return out


def scenario_queue():
    """Bulk backfill burst + steady interactive jobs: one shared pool vs bulkheads with an autoscaler."""
    rng = random.Random(6)
    jobs = [(round(t, 3), "interactive", 0.5) for t in poisson(rng, 1.5, 0, 240)]
    jobs += [(round(t, 3), "bulk", 2.0) for t in poisson(rng, 20, 0, 30)]
    jobs.sort()
    out = {}
    for mode in ("off_shared_fixed_2", "on_bulkheads_autoscaled"):
        clock = FakeClock(0)
        r = fakeredis.FakeRedis()
        ids = itertools.count()
        if mode.startswith("off"):
            pools = {"shared": {"max_backlog": 10_000, "max_wait_s": 900.0}}
        else:
            pools = {"interactive": {"max_backlog": 200, "max_wait_s": 30.0},
                     "bulk": {"max_backlog": 2000, "max_wait_s": 900.0}}
        q = JobQueue(r, clock=clock, pools=pools, id_factory=lambda ids=ids: f"j{next(ids):06d}")
        meta = {}
        scalers = {}
        if mode.startswith("off"):
            workers = {"shared": [0.0, 0.0]}
        else:
            scalers = {"interactive": Autoscaler(warm_min=1, max_workers=4, jobs_per_worker=5, startup_s=5),
                       "bulk": Autoscaler(warm_min=1, max_workers=12, jobs_per_worker=20, max_oldest_age_s=30,
                                          startup_s=10)}
            workers = {p: [0.0] for p in pools}
        waits = {"interactive": [], "bulk": []}
        done_at = {"interactive": 0.0, "bulk": 0.0}
        peak = {p: len(w) for p, w in workers.items()}
        ji = 0
        dt = 0.1
        t = 0.0
        while t < 1200:
            clock.t = t
            while ji < len(jobs) and jobs[ji][0] <= t:
                at, kind, dur = jobs[ji]
                pool = "shared" if mode.startswith("off") else kind
                jid = q.enqueue(pool, {"kind": kind, "dur": dur, "at": at}, priority="normal")
                meta[jid] = (kind, dur)
                ji += 1
            if scalers and int(t * 10) % 10 == 0:
                for p, sc in scalers.items():
                    n = sc.step(t, q.depth(p), q.oldest_age(p))
                    while len(workers[p]) < n:
                        workers[p].append(t)
                    if len(workers[p]) > n:
                        workers[p] = sorted(workers[p])[:n]
                    peak[p] = max(peak[p], n)
            for p, ws in workers.items():
                for i, busy in enumerate(ws):
                    if busy <= t:
                        job = q.claim(p)
                        if job is None:
                            break
                        kind, dur = meta[job.job_id]
                        waits[kind].append(t - job.enqueued)
                        ws[i] = t + dur
                        done_at[kind] = max(done_at[kind], t + dur)
                        q.ack(job)
            if ji >= len(jobs) and all(q.depth(p) == 0 for p in pools) and \
                    all(b <= t for ws in workers.values() for b in ws):
                break
            t = round(t + dt, 1)
        dlq = sum(len(q.dlq(p)) for p in pools)
        out[mode] = {
            "interactive_jobs": sum(1 for j in jobs if j[1] == "interactive"),
            "bulk_jobs": sum(1 for j in jobs if j[1] == "bulk"),
            "interactive_wait_p95_s": r3(percentile(waits["interactive"], 95)),
            "interactive_wait_max_s": r3(max(waits["interactive"])),
            "bulk_drain_time_s": r3(done_at["bulk"]),
            "dead_lettered": dlq,
            "peak_workers": peak,
        }
        if scalers:
            out[mode]["scale_events"] = {p: len(sc.decisions) for p, sc in scalers.items()}
    return out


def run_all() -> dict:
    return {
        "_note": "Deterministic model run in virtual time with the repo's real components and fake providers. "
                 "Not a hardware benchmark. Regenerate: python -m loadtest.harness",
        "rate_limiting": scenario_rate_limit(),
        "semantic_cache": scenario_cache(),
        "intent_router": scenario_router(),
        "provider_outage": scenario_outage(),
        "replica_failure": scenario_replica_failure(),
        "queue_and_autoscaling": scenario_queue(),
    }


def main(argv) -> int:
    res = json.dumps(run_all(), indent=2, sort_keys=True) + "\n"
    if "--check" in argv:
        if not OUT.exists() or OUT.read_text() != res:
            print("loadtest/results/results.json is stale; run python -m loadtest.harness")
            return 1
        print("load results match a fresh run")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(res)
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
