"""Compose entrypoints: `python -m sysdesign.worker autoscaler` and `python -m sysdesign.worker pool <name>`.

The autoscaler process reads queue depth and oldest-task age from Redis and
publishes the desired worker count per pool; each pool process keeps that
many worker threads running (warm minimum always kept). Workers reap
expired leases so jobs from a dead process are resumed from their checkpoint.
"""
from __future__ import annotations

import os
import sys
import threading
import time

from .autoscaler import Autoscaler
from .checkpoints import CheckpointStore
from .jobqueue import JobQueue
from .tools import Commerce


def _deps():  # pragma: no cover - compose runtime only
    import redis

    r = redis.Redis.from_url(os.environ["REDIS_URL"])
    store = CheckpointStore.postgres(os.environ["POSTGRES_DSN"])
    return r, store


def desired_key(pool: str) -> str:
    return f"scale:desired:{pool}"


def autoscale_tick(r, queue: JobQueue, scalers: dict, now: float) -> dict:
    out = {}
    for pool, sc in scalers.items():
        n = sc.step(now, queue.depth(pool), queue.oldest_age(pool))
        r.set(desired_key(pool), n)
        out[pool] = n
    return out


def main(argv) -> int:  # pragma: no cover - compose runtime only
    r, store = _deps()
    q = JobQueue(r)
    if argv[1] == "autoscaler":
        scalers = {"interactive": Autoscaler(warm_min=1, max_workers=4, jobs_per_worker=5),
                   "bulk": Autoscaler(warm_min=1, max_workers=8, jobs_per_worker=20)}
        while True:
            print(autoscale_tick(r, q, scalers, time.time()), flush=True)
            time.sleep(2)
    pool = argv[2]
    shop = Commerce(r)  # shared with the API replicas through Redis
    threads: list[threading.Event] = []

    def loop(stop: threading.Event, name: str):
        from .app import run_worker_once

        while not stop.is_set():
            q.reap(pool)
            if run_worker_once(q, store, shop, name, pool=pool) is None:
                time.sleep(0.5)

    while True:
        want = int(r.get(desired_key(pool)) or 1)
        while len(threads) < want:
            ev = threading.Event()
            threading.Thread(target=loop, args=(ev, f"{pool}-{len(threads)}"), daemon=True).start()
            threads.append(ev)
        while len(threads) > max(1, want):
            threads.pop().set()
        time.sleep(1)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv))
