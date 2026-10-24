"""Distributed-systems choices: CAP per store, stateful routing (sticky + consistent hashing with
bounded loads), request batching, and data-residency routing.

Memcached vs Redis: memcached is a plain multi-threaded LRU cache (no persistence, no data types);
Redis adds atomic scripts, sorted sets and persistence, which the limiter and queue rely on.
"""
from __future__ import annotations

import asyncio
import bisect
import hashlib
import math
import time
from dataclasses import dataclass, field


# ---------------------------------------------------------------- CAP per store
class PartitionError(Exception):
    pass


@dataclass
class Replica:
    data: dict = field(default_factory=dict)
    reachable: bool = True


@dataclass
class ReplicatedStore:
    """mode='CP': writes/reads need a majority, else fail closed (refunds, fraud-like actions).
    mode='AP': serve whatever the local replica has, possibly stale (recommendations)."""
    mode: str
    replicas: list[Replica] = field(default_factory=lambda: [Replica(), Replica(), Replica()])

    def _quorum(self) -> list[Replica]:
        up = [r for r in self.replicas if r.reachable]
        return up if len(up) * 2 > len(self.replicas) else []

    def write(self, k, v, local: int = 0) -> bool:
        if self.mode == "CP":
            q = self._quorum()
            if not q:
                raise PartitionError("no quorum: refusing write")
            for r in q:
                r.data[k] = v
            return True
        self.replicas[local].data[k] = v           # AP: accept locally, reconcile later
        return True

    def read(self, k, local: int = 0):
        if self.mode == "CP":
            q = self._quorum()
            if not q:
                raise PartitionError("no quorum: refusing read")
            vals = [r.data.get(k) for r in q]
            return max(vals, key=lambda v: vals.count(v))
        return self.replicas[local].data.get(k)


# ---------------------------------------------------------------- stateful routing
def _h(s: str) -> int:
    return int(hashlib.sha256(s.encode()).hexdigest()[:16], 16)


class BoundedConsistentHash:
    """Consistent hashing with bounded loads: a key goes to its ring owner unless that node already
    holds more than ceil(c * average) active keys, then to the next node on the ring."""

    def __init__(self, nodes: list[str], c: float = 1.25, vnodes: int = 64) -> None:
        self.c = c
        self.ring = sorted((_h(f"{n}#{i}"), n) for n in nodes for i in range(vnodes))
        self.keys = [h for h, _ in self.ring]
        self.load = {n: 0 for n in nodes}
        self.assigned: dict[str, str] = {}

    def cap(self) -> int:
        total = sum(self.load.values()) + 1
        return math.ceil(self.c * total / len(self.load))

    def assign(self, key: str) -> str:
        if key in self.assigned:
            return self.assigned[key]
        i = bisect.bisect(self.keys, _h(key)) % len(self.ring)
        cap = self.cap()
        for j in range(len(self.ring)):
            node = self.ring[(i + j) % len(self.ring)][1]
            if self.load[node] < cap:
                self.load[node] += 1
                self.assigned[key] = node
                return node
        raise RuntimeError("no capacity")

    def release(self, key: str) -> None:
        node = self.assigned.pop(key, None)
        if node:
            self.load[node] -= 1


def prefix_cache_hit_rate(router: str, sessions: int = 200, turns: int = 5, nodes: int = 4, seed: int = 1) -> dict:
    """Each session's prompt prefix is cached on the node that served it. Round-robin scatters turns;
    sticky CHWBL keeps a session on one node, so later turns hit the prefix cache."""
    import random
    rng = random.Random(seed)
    names = [f"gpu-{i}" for i in range(nodes)]
    ch = BoundedConsistentHash(names)
    cached: dict[str, set] = {n: set() for n in names}
    hits = total = 0
    rr = 0
    order = [(s, t) for t in range(turns) for s in range(sessions)]
    rng.shuffle(order)
    for s, _t in order:
        key = f"session-{s}"
        if router == "round_robin":
            node = names[rr % nodes]
            rr += 1
        else:
            node = ch.assign(key)
        total += 1
        if key in cached[node]:
            hits += 1
        cached[node].add(key)
    loads = ch.load if router != "round_robin" else {n: sessions / nodes for n in names}
    return {"router": router, "hit_rate": round(hits / total, 4), "max_load": max(loads.values()),
            "avg_load": round(sum(loads.values()) / nodes, 2)}


# ---------------------------------------------------------------- batching
async def batching_benchmark(batch_size: int, n_requests: int = 256, arrival_gap_s: float = 0.0005,
                             fixed_cost_s: float = 0.004, per_item_s: float = 0.0004, max_wait_s: float = 0.006):
    """Simulated accelerator: a forward pass costs fixed + per_item * batch. Bigger batches raise
    throughput but each request may wait for the batch to fill (latency)."""
    q: asyncio.Queue = asyncio.Queue()
    lat: list[float] = []

    async def server():
        while len(lat) < n_requests:
            first = await q.get()
            batch = [first]
            deadline = time.perf_counter() + max_wait_s
            while len(batch) < batch_size:
                timeout = deadline - time.perf_counter()
                if timeout <= 0:
                    break
                try:
                    batch.append(await asyncio.wait_for(q.get(), timeout))
                except TimeoutError:
                    break
            await asyncio.sleep(fixed_cost_s + per_item_s * len(batch))
            now = time.perf_counter()
            for t0, fut in batch:
                lat.append(now - t0)
                fut.set_result(None)

    async def client():
        loop = asyncio.get_running_loop()
        futs = []
        for _ in range(n_requests):
            f = loop.create_future()
            await q.put((time.perf_counter(), f))
            futs.append(f)
            await asyncio.sleep(arrival_gap_s)
        await asyncio.gather(*futs)

    t0 = time.perf_counter()
    srv = asyncio.create_task(server())
    await asyncio.gather(client(), srv)
    wall = time.perf_counter() - t0
    from .observability import percentile
    return {"batch_size": batch_size, "p50_ms": round(percentile(lat, 50) * 1000, 2),
            "p95_ms": round(percentile(lat, 95) * 1000, 2), "throughput_rps": round(n_requests / wall, 1)}


# ---------------------------------------------------------------- residency
REGIONS = {"eu": ["eu-replica-1"], "in": ["in-replica-1"], "us": ["us-replica-1", "us-replica-2"]}
STRICT = {"eu", "in"}


class ResidencyRouter:
    """Region comes from the VERIFIED token claim; a missing/unknown region defaults to 'us' only for
    non-strict tenants. Strict regions never fail over to another region (fail closed instead)."""

    def __init__(self) -> None:
        self.down: set[str] = set()
        self.log: list[tuple[str, str]] = []

    def route(self, identity: dict) -> str:
        region = identity.get("region", "us")
        if region not in REGIONS:
            raise PermissionError("unknown region")
        healthy = [r for r in REGIONS[region] if r not in self.down]
        if healthy:
            self.log.append((region, healthy[0]))
            return healthy[0]
        if region in STRICT:
            raise PermissionError(f"no in-region replica for {region}; not failing over")
        alt = next(r for g, rs in REGIONS.items() if g not in STRICT for r in rs if r not in self.down)
        self.log.append((region, alt))
        return alt
