"""Talk 17 add-on to backend-evolution step 4: where session state lives behind a balancer.

Three replicas behind round-robin (or sticky) routing, three setups:
- memory: each replica keeps sessions in its own dict (InProcessSessionStore);
- sticky: same dicts, but the balancer pins a user to one replica by hash;
- shared: every replica reads and writes one Redis (fakeredis in tests) through the same interface.
Simulated sessions; counts are exact for the seeded replay.
"""
from __future__ import annotations

import hashlib
import json
import random
import statistics

from .backend import InProcessSessionStore


class RedisSessionStore:
    def __init__(self, redis, prefix: str = "sess") -> None:
        self.r, self.p = redis, prefix

    def put(self, k, v):
        self.r.set(f"{self.p}:{k}", json.dumps(v))

    def get(self, k):
        v = self.r.get(f"{self.p}:{k}")
        return json.loads(v) if v is not None else None

    def incr(self, k) -> int:
        return int(self.r.incr(f"{self.p}:n:{k}"))


class Replica:
    def __init__(self, name: str, store) -> None:
        self.name, self.store, self.alive, self.served = name, store, True, 0
        self._hits = 0

    def hit_counter(self) -> int:
        if isinstance(self.store, RedisSessionStore):
            return self.store.incr("hits")
        self._hits += 1
        return self._hits


class Balancer:
    def __init__(self, replicas: list[Replica], sticky: bool = False) -> None:
        self.replicas, self.sticky, self._i = replicas, sticky, 0

    def pick(self, user: str) -> Replica:
        alive = [r for r in self.replicas if r.alive]
        if self.sticky:
            pinned = self.replicas[int(hashlib.sha256(user.encode()).hexdigest(), 16) % len(self.replicas)]
            if pinned.alive:
                return pinned
            return alive[int(hashlib.sha256(user.encode()).hexdigest(), 16) % len(alive)]  # re-pinned, state gone
        r = alive[self._i % len(alive)]
        self._i += 1
        return r


def build(setup: str, redis=None, n: int = 3) -> Balancer:
    if setup == "shared":
        if redis is None:
            import fakeredis
            redis = fakeredis.FakeRedis()
        reps = [Replica(f"r{i + 1}", RedisSessionStore(redis)) for i in range(n)]
    else:
        reps = [Replica(f"r{i + 1}", InProcessSessionStore()) for i in range(n)]
    return Balancer(reps, sticky=(setup == "sticky"))


FORM_SECTIONS = ["contact", "address", "payment_method", "review"]


def replay(setup: str, users: int = 60, steps: int = 12, kill_at: int | None = None,
           heavy_user_x: int = 1, seed: int = 5) -> dict:
    """Each user logs in, then mixes cart adds and form sections. A request that does not find the
    session counts as a logout; cart items and form sections written earlier but missing at the end
    count as lost."""
    rng = random.Random(seed)
    bal = build(setup)
    names = [f"u{i:03d}" for i in range(users)]
    expected = {u: {"cart": 0, "form": 0} for u in names}
    logouts = 0
    script = []
    for step in range(steps):
        batch = []
        for u in names:
            batch += [(step, u)] * (heavy_user_x if u == names[0] else 1)
        rng.shuffle(batch)  # arrival order varies, as it does in real traffic
        script += batch
    seen_login = set()
    for step, u in script:
        if kill_at is not None and step == kill_at:
            for r in bal.replicas:
                if r.name == "r2" and r.alive:
                    r.alive = False
        rep = bal.pick(u)
        rep.served += 1
        if u not in seen_login:
            rep.store.put(u, {"user": u, "cart": [], "form": {}})
            seen_login.add(u)
            continue
        sess = rep.store.get(u)
        if sess is None:
            logouts += 1
            sess = {"user": u, "cart": [], "form": {}}  # forced to log in again, starts empty
        if rng.random() < 0.6:
            sess["cart"].append(f"SKU-{rng.randint(1, 50):03d}")
            expected[u]["cart"] += 1
        else:
            sec = FORM_SECTIONS[expected[u]["form"] % len(FORM_SECTIONS)]
            sess["form"][f"{sec}-{expected[u]['form']}"] = "filled"
            expected[u]["form"] += 1
        rep.store.put(u, sess)
    lost_cart = lost_form = 0
    for u in names:
        best = None
        for r in bal.replicas:
            if r.alive:
                s = r.store.get(u)
                if s is not None and (best is None or len(s["cart"]) + len(s["form"]) > len(best["cart"]) + len(best["form"])):
                    best = s
        have_c = len(best["cart"]) if best else 0
        have_f = len(best["form"]) if best else 0
        lost_cart += expected[u]["cart"] - have_c
        lost_form += expected[u]["form"] - have_f
    served = [r.served for r in bal.replicas]
    return {"setup": setup, "requests": len(script), "logouts": logouts, "lost_cart_items": lost_cart,
            "lost_form_sections": lost_form,
            "cart_items_added": sum(e["cart"] for e in expected.values()),
            "form_sections_filled": sum(e["form"] for e in expected.values()),
            "served": served, "max_over_avg": round(max(served) / statistics.fmean(served), 3)}


def counter_demo(setup: str, hits: int = 9) -> dict:
    bal = build(setup)
    last = []
    for _ in range(hits):
        last.append(bal.pick("visitor").hit_counter())
    per = [r._hits for r in bal.replicas] if setup != "shared" else None
    return {"setup": setup, "values_returned": last, "final": last[-1], "per_replica": per}


def results() -> dict:
    out = {"note": "simulated sessions, seeded; 3 replicas", "plain": [], "replica_dies": [], "heavy_user": []}
    for s in ("memory", "sticky", "shared"):
        out["plain"].append(replay(s))
        out["replica_dies"].append(replay(s, kill_at=6))
        out["heavy_user"].append(replay(s, users=30, heavy_user_x=10))
    out["counter"] = [counter_demo("memory"), counter_demo("shared")]
    return out
