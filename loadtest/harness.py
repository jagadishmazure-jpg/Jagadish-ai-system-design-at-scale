"""Async load harness: each pattern OFF vs ON, in-process (ASGI transport, fake providers).

SIMULATED SCALE: one Python process, fake model latency (10-50 ms), hundreds of requests, not
thousands of real users. Numbers show the direction and size of each pattern's effect under the
same simulated load; they are not production capacity claims.

python -m loadtest.harness --scale ci     # small, runs in CI
python -m loadtest.harness --scale full   # committed results/loadtest_full.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import random
import time

import httpx

from sysdesign.admission import Limits
from sysdesign.app import create_app
from sysdesign.autoscaler import Autoscaler
from sysdesign.build import build
from sysdesign.gateway import LocalIssuer
from sysdesign.jobqueue import JobQueue, PoolConfig
from sysdesign.llm import default_providers
from sysdesign.observability import percentile

ROOT = pathlib.Path(__file__).resolve().parent.parent
FAQ = ["Do you ship internationally?", "do you ship internationally", "Do you ship internationally please?",
       "what is the warranty on electronics", "What is the warranty on electronics?",
       "can I change my delivery address", "Can I change my delivery address?", "do you price match",
       "Do you price match?", "how long until my refund shows up"]
MIX = ["hi", "Where is my order QM-10002?", "Do you ship internationally?", "what is the warranty on electronics",
       "Compare standard and express shipping and explain why express shipping costs more, and how international shipping duties work?",
       "I was charged twice", "can I change my delivery address", "Where is my order QM-10001?"]
SCALES = {"ci": {"users": 10, "per_user": 6, "abuse": 80, "faq": 80, "mix": 80, "outage_n": 80, "jobs": 150},
          "full": {"users": 40, "per_user": 10, "abuse": 600, "faq": 400, "mix": 400, "outage_n": 300, "jobs": 600}}


def stats(lat: list[float]) -> dict:
    return {"n": len(lat), "p50_ms": round(percentile(lat, 50) * 1000, 2), "p95_ms": round(percentile(lat, 95) * 1000, 2),
            "p99_ms": round(percentile(lat, 99) * 1000, 2)}


class Client:
    def __init__(self, system, issuer=None) -> None:
        self.issuer = issuer or LocalIssuer()
        self.app = create_app(system, self.issuer)
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://lb", timeout=30)
        self.tokens: dict[str, str] = {}

    def auth(self, user: str) -> dict:
        if user not in self.tokens:
            self.tokens[user] = self.issuer.token(user)
        return {"Authorization": "Bearer " + self.tokens[user]}

    async def chat(self, user: str, msg: str):
        t0 = time.perf_counter()
        r = await self.http.post("/v1/chat", json={"message": msg, "conversation_id": user}, headers=self.auth(user))
        return r.status_code, time.perf_counter() - t0, (r.json() if r.status_code == 200 else None)


def tight_providers(seed=7):
    p = default_providers(seed)
    for v in p.values():
        v.soft_capacity, v.hard_capacity = 8, 24      # a small provider quota so overload is reachable
    return p


async def noisy_neighbor(admission: bool, sc: dict) -> dict:
    sys_ = build(admission=admission, cache=False, providers=tight_providers(),
                 limits=Limits(user_rps=5, user_burst=10, max_concurrent_model_calls=16))
    c = Client(sys_)
    normal_lat, normal_err, abuse_429, abuse_ok = [], 0, 0, 0

    async def normal(u):
        nonlocal normal_err
        for i in range(sc["per_user"]):
            code, dt, _ = await c.chat(f"u-n{u}", FAQ[(u + i) % len(FAQ)])
            if code == 200:
                normal_lat.append(dt)
            elif code != 429:
                normal_err += 1
            await asyncio.sleep(0.02)

    async def abuser(i):
        nonlocal abuse_429, abuse_ok
        code, _, _ = await c.chat("u-abuser", FAQ[i % len(FAQ)])
        if code == 429:
            abuse_429 += 1
        elif code == 200:
            abuse_ok += 1

    errs = 0

    async def guarded(coro):
        nonlocal errs
        try:
            await coro
        except Exception:  # noqa: BLE001 - provider overload surfaces as a 500 in naive mode
            errs += 1

    t0 = time.perf_counter()
    await asyncio.gather(*[guarded(abuser(i)) for i in range(sc["abuse"])],
                         *[guarded(normal(u)) for u in range(sc["users"])])
    wall = time.perf_counter() - t0
    total_normal = sc["users"] * sc["per_user"]
    return {"admission": admission, "normal_users": stats(normal_lat),
            "normal_success_rate": round(len(normal_lat) / total_normal, 4),
            "abuser_requests": sc["abuse"], "abuser_429_rate": round(abuse_429 / sc["abuse"], 4),
            "server_errors": errs + normal_err, "provider_calls": sum(p.calls for p in sys_.gateway.providers.values()),
            "wall_s": round(wall, 3)}


async def cache_test(enabled: bool, sc: dict) -> dict:
    sys_ = build(admission=False, cache=enabled)
    c = Client(sys_)
    lat, cost = [], 0.0
    sem = asyncio.Semaphore(16)

    async def one(i):
        nonlocal cost
        async with sem:
            code, dt, body = await c.chat(f"u-c{i % 7}", FAQ[i % len(FAQ)])
        lat.append(dt)
    await asyncio.gather(*[one(i) for i in range(sc["faq"])])
    cost = sum(t.cost_usd for t in sys_.agent.traces.traces.values())
    return {"cache": enabled, **stats(lat), "hit_rate": round(sys_.cache.hit_rate(), 4),
            "model_calls": sum(p.calls for p in sys_.gateway.providers.values()), "cost_usd": round(cost, 6)}


async def router_test(enabled: bool, sc: dict) -> dict:
    sys_ = build(admission=False, cache=False, router=enabled)
    c = Client(sys_)
    lat = []
    sem = asyncio.Semaphore(16)

    async def one(i):
        async with sem:
            code, dt, body = await c.chat(f"u-r{i % 9}", MIX[i % len(MIX)])
        lat.append(dt)
    await asyncio.gather(*[one(i) for i in range(sc["mix"])])
    traces = list(sys_.agent.traces.traces.values())
    cost = sum(t.cost_usd for t in traces)
    return {"router": enabled, **stats(lat), "cost_usd": round(cost, 6),
            "cost_per_request_usd": round(cost / len(traces), 8),
            "large_model_calls": sys_.gateway.providers["large"].calls,
            "small_model_calls": sys_.gateway.providers["small"].calls}


async def outage_test(resilient: bool, sc: dict) -> dict:
    """Large-model provider goes down (hangs 300 ms then errors) for the middle third of the run."""
    p = default_providers()
    p["large"].hang_s = 0.3
    sys_ = build(admission=False, cache=False, resilience=resilient, providers=p)
    sys_.gateway.timeout_s = 0.15
    sys_.gateway.breaker_cooldown_s = 0.25
    c = Client(sys_)
    q = "Compare standard and express shipping and explain why express shipping costs more, and how international shipping duties work?"
    n = sc["outage_n"]
    lat, errors, degraded = [], 0, 0
    outage_end = None
    primary_ok_after = None
    for i in range(n):
        if i == n // 3:
            p["large"].outage = True
        if i == 2 * n // 3:
            p["large"].outage = False
            outage_end = time.perf_counter()
        calls_before = p["large"].calls
        try:
            code, dt, body = await c.chat("u-o", q)
        except Exception:  # noqa: BLE001 - naive mode: provider error bubbles up as a 500
            code, dt, body = 500, 0.3, None
        lat.append(dt)
        if code != 200:
            errors += 1
        elif "trouble reaching" in body["answer"]:
            degraded += 1
        if outage_end and primary_ok_after is None and p["large"].calls > calls_before and code == 200 and \
                body and "trouble" not in body["answer"] and not sys_.gateway.fallbacks_used_last(i):
            primary_ok_after = time.perf_counter() - outage_end
        await asyncio.sleep(0.01)
    return {"resilience": resilient, **stats(lat), "error_rate": round(errors / n, 4),
            "degraded_answers": degraded, "fallbacks_used": sys_.gateway.fallbacks_used,
            "recovery_s": round(primary_ok_after, 3) if primary_ok_after is not None else None,
            "breaker_transitions": sys_.gateway.breaker("large").transitions if resilient else []}


async def queue_drain(autoscale: bool, sc: dict) -> dict:
    import fakeredis
    q = JobQueue(fakeredis.FakeRedis(), {"reports": PoolConfig(max_backlog=10_000)})
    for i in range(sc["jobs"]):
        q.enqueue("reports", {"i": i}, f"k{i}")
    scaler = Autoscaler(warm_min=1, max_workers=16, target_jobs_per_worker=20, max_age_s=0.5)
    workers: list[asyncio.Task] = []
    done = 0
    stop = asyncio.Event()

    async def worker(idx):
        nonlocal done
        while not stop.is_set():
            if idx >= scaler.current and autoscale:
                return
            j = q.claim("reports")
            if j is None:
                await asyncio.sleep(0.005)
                continue
            await asyncio.sleep(0.01)          # simulated job work
            q.ack("reports", j["job_id"])
            done += 1

    t0 = time.perf_counter()
    peak = 1
    workers.append(asyncio.create_task(worker(0)))
    while done < sc["jobs"]:
        if autoscale:
            want = scaler.decide(time.perf_counter(), q.depth("reports"), q.oldest_age_any("reports"))
            while len([w for w in workers if not w.done()]) < want:
                workers.append(asyncio.create_task(worker(len(workers))))
            peak = max(peak, want)
        await asyncio.sleep(0.02)
    stop.set()
    await asyncio.gather(*workers)
    return {"autoscale": autoscale, "jobs": sc["jobs"], "drain_s": round(time.perf_counter() - t0, 3),
            "peak_workers": peak}


async def latency_doubling(sc: dict) -> dict:
    out = {}
    for factor in (1, 2):
        p = default_providers()
        for v in p.values():
            v.base_latency_s *= factor
            v.per_token_s *= factor
        sys_ = build(admission=False, cache=False, providers=p)
        c = Client(sys_)
        lat = []
        for i in range(sc["mix"] // 2):
            _, dt, _ = await c.chat("u-l", MIX[i % len(MIX)])
            lat.append(dt)
        out[f"x{factor}"] = stats(lat)
    return out


async def stage_timings(sc: dict) -> dict:
    """Per-stage time from traces vs the latency sub-budgets in docs/requirements.md."""
    from sysdesign.agent import Agent
    sys_ = build(admission=False, cache=False)
    c = Client(sys_)
    stages: dict[str, list[float]] = {"gateway_and_admission": [], "retrieval": [], "inference": [], "total": []}
    orig = Agent.handle

    async def timed(self, text, identity, conversation_id="c"):
        t0 = time.perf_counter()
        r = await orig(self, text, identity, conversation_id)
        r["_agent_s"] = time.perf_counter() - t0
        return r
    Agent.handle = timed
    try:
        for i in range(sc["faq"] // 2):
            t0 = time.perf_counter()
            r = await c.http.post("/v1/chat", json={"message": FAQ[i % len(FAQ)]}, headers=c.auth("u-s"))
            total = time.perf_counter() - t0
            tr = list(sys_.agent.traces.traces.values())[-1]
            tool = sum(s.latency_s for s in tr.steps if s.tool == "search_policy")
            stages["retrieval"].append(tool)
            stages["total"].append(total)
            agent_s = r.json()["latency_s"]
            stages["gateway_and_admission"].append(max(0.0, total - agent_s))
            stages["inference"].append(max(0.0, agent_s - tool))
    finally:
        Agent.handle = orig
    return {k: {"p50_ms": round(percentile(v, 50) * 1000, 3), "p95_ms": round(percentile(v, 95) * 1000, 3)}
            for k, v in stages.items()}


async def main(scale: str) -> dict:
    sc = SCALES[scale]
    random.seed(1)
    res = {"scale": scale, "note": "simulated: in-process, fake providers; directional, not capacity claims",
           "inputs": sc}
    res["noisy_neighbor"] = {"off": await noisy_neighbor(False, sc), "on": await noisy_neighbor(True, sc)}
    res["semantic_cache"] = {"off": await cache_test(False, sc), "on": await cache_test(True, sc)}
    res["router"] = {"off": await router_test(False, sc), "on": await router_test(True, sc)}
    res["provider_outage"] = {"off": await outage_test(False, sc), "on": await outage_test(True, sc)}
    res["queue_drain"] = {"off": await queue_drain(False, sc), "on": await queue_drain(True, sc)}
    res["latency_doubling"] = await latency_doubling(sc)
    res["stage_timings"] = await stage_timings(sc)
    return res


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", choices=list(SCALES), default="ci")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    r = asyncio.run(main(a.scale))
    out = pathlib.Path(a.out) if a.out else ROOT / "results" / f"loadtest_{a.scale}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(r, indent=1) + "\n")
    print(json.dumps({k: v for k, v in r.items() if k not in ("inputs",)}, indent=1))
