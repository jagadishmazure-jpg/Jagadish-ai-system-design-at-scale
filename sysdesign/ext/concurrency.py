"""Talk 18 (C): concurrency vs parallelism. Why a loop that holds thousands of idle chats falls over on
100 image uploads, and what fixes it.

Story (fictional): the chat app for the made-up Brightwell Learning platform ran a year on one asyncio
loop with 10,000+ open chats. A new "upload a photo" feature resized and boosted contrast on the loop
itself. On launch day ~100 uploads froze every chat.

Everything here runs for real on the machine (no simulation); the numbers depend on its CPU count and
Python build, which every result records.
"""
from __future__ import annotations

import asyncio
import os
import platform
import statistics
import sys
import sysconfig
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

# --------------------------------------------------------------------------- CPU-heavy work


def count_primes(limit: int) -> int:
    """Pure-Python trial division: holds the GIL the whole time, never waits on I/O."""
    n = 0
    for x in range(2, limit):
        r = int(x ** 0.5)
        for d in range(2, r + 1):
            if x % d == 0:
                break
        else:
            n += 1
    return n


def image_preprocess(seed: int, w: int = 640, h: int = 480) -> int:
    """Stand-in for resize + contrast on an uploaded photo, in pure Python (no C library releases the GIL).
    Returns a checksum so the work cannot be optimised away."""
    px = [(seed * 7919 + i * 104729) % 256 for i in range(w * h)]
    small = [(px[y * w + x] + px[y * w + x + 1] + px[(y + 1) * w + x] + px[(y + 1) * w + x + 1]) // 4
             for y in range(0, h - 1, 2) for x in range(0, w - 1, 2)]  # 2x box downscale
    lo, hi = min(small), max(small)
    span = max(1, hi - lo)
    out = [(v - lo) * 255 // span for v in small]  # contrast stretch
    return sum(out) % 1_000_003


def machine() -> dict:
    gil = getattr(sys, "_is_gil_enabled", lambda: True)()
    return {"cpu_count": os.cpu_count(), "python": platform.python_version(),
            "gil_enabled": gil, "free_threaded_build": bool(sysconfig.get_config_var("Py_GIL_DISABLED"))}


# --------------------------------------------------------------------------- heartbeat probe


class Heartbeat:
    """Wakes every `interval` seconds and records how late it woke. Lateness = time the loop was stuck."""

    def __init__(self, interval: float = 0.005):
        self.interval, self.lags = interval, []
        self._task = None

    async def _run(self):
        while True:
            t = time.perf_counter()
            await asyncio.sleep(self.interval)
            self.lags.append(max(0.0, time.perf_counter() - t - self.interval))

    def start(self):
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> dict:
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        lags = sorted(self.lags) or [0.0]
        return {"max_stall_ms": round(lags[-1] * 1e3, 1), "p99_lag_ms": round(lags[int(0.99 * (len(lags) - 1))] * 1e3, 1),
                "beats": len(lags)}


# --------------------------------------------------------------------------- the replay


async def _replay(idle: int, jobs: int, mode: str, pool) -> dict:
    loop = asyncio.get_running_loop()

    async def handle(r: asyncio.StreamReader, w: asyncio.StreamWriter):
        try:
            while line := await r.readline():
                if line.startswith(b"img"):
                    seed = int(line.split()[1])
                    if mode == "inline":
                        out = image_preprocess(seed)  # the bug: CPU work on the event loop
                    else:
                        out = await loop.run_in_executor(pool, image_preprocess, seed)
                    w.write(f"{out}\n".encode())
                else:
                    w.write(b"pong\n")
                await w.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            w.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, backlog=4096)
    port = server.sockets[0].getsockname()[1]
    conns = [await asyncio.open_connection("127.0.0.1", port) for _ in range(idle)]
    hb = Heartbeat()
    hb.start()
    await asyncio.sleep(0.3)
    idle_stats = await hb.stop()

    # chat pings on one idle connection while the uploads run
    cr, cw = conns[0]
    pings: list[float] = []
    done = asyncio.Event()

    async def chatter():
        while not done.is_set():
            t = time.perf_counter()
            cw.write(b"ping\n")
            await cw.drain()
            await cr.readline()
            pings.append(time.perf_counter() - t)
            await asyncio.sleep(0.01)

    async def upload(i: int):
        r, w = conns[1 + i % (idle - 1)]
        w.write(f"img {i}\n".encode())
        await w.drain()
        await r.readline()

    hb = Heartbeat()
    hb.start()
    chat = asyncio.create_task(chatter())
    t0 = time.perf_counter()
    # one upload per connection at a time (a chat client waits for its reply)
    by_conn: dict[int, list[int]] = {}
    for i in range(jobs):
        by_conn.setdefault(1 + i % (idle - 1), []).append(i)

    async def conn_uploads(ids):
        for i in ids:
            await upload(i)

    await asyncio.gather(*(conn_uploads(v) for v in by_conn.values()))
    wall = time.perf_counter() - t0
    done.set()
    await chat
    load_stats = await hb.stop()
    for _, w in conns:
        w.close()
    server.close()
    await server.wait_closed()
    pings.sort()
    return {"mode": mode, "idle_connections": idle, "jobs": jobs, "idle_loop": idle_stats, "during_uploads": load_stats,
            "chat_ping_ms": {"p50": round(statistics.median(pings) * 1e3, 1) if pings else None,
                             "max": round(pings[-1] * 1e3, 1) if pings else None, "count": len(pings)},
            "upload_wall_s": round(wall, 3), "throughput_jobs_per_s": round(jobs / wall, 1)}


def replay(idle: int = 2000, jobs: int = 100, mode: str = "inline", workers: int | None = None) -> dict:
    pool = ProcessPoolExecutor(max_workers=workers or os.cpu_count()) if mode == "process" else None
    try:
        if pool:
            list(pool.map(image_preprocess, range(pool._max_workers)))  # warm the workers before timing
        return asyncio.run(_replay(idle, jobs, mode, pool))
    finally:
        if pool:
            pool.shutdown()


# --------------------------------------------------------------------------- the talk's experiments


def io_experiment(n: int = 5000, sleep_s: float = 0.05, threads: int = 256) -> dict:
    t = time.perf_counter()
    with ThreadPoolExecutor(threads) as ex:
        list(ex.map(lambda _: time.sleep(sleep_s), range(n)))
    th = time.perf_counter() - t

    async def many():
        await asyncio.gather(*(asyncio.sleep(sleep_s) for _ in range(n)))

    t = time.perf_counter()
    asyncio.run(many())
    asy = time.perf_counter() - t
    return {"tasks": n, "sleep_s": sleep_s, "threads": threads, "thread_pool_s": round(th, 3),
            "asyncio_s": round(asy, 3), "ideal_thread_s": round(-(-n // threads) * sleep_s, 3)}


def cpu_experiment(n: int = 150, limit: int = 20000, threads: int = 24, workers: int | None = None) -> dict:
    workers = workers or os.cpu_count()
    t = time.perf_counter()
    for _ in range(n):
        count_primes(limit)
    serial = time.perf_counter() - t

    t = time.perf_counter()
    with ThreadPoolExecutor(threads) as ex:
        list(ex.map(count_primes, [limit] * n))
    thr = time.perf_counter() - t

    async def as_async():
        async def one():
            return count_primes(limit)  # no await inside: it never yields
        await asyncio.gather(*(one() for _ in range(n)))

    t = time.perf_counter()
    asyncio.run(as_async())
    asy = time.perf_counter() - t

    with ProcessPoolExecutor(workers) as ex:
        list(ex.map(count_primes, [100] * workers))  # start workers outside the timing
        t = time.perf_counter()
        list(ex.map(count_primes, [limit] * n, chunksize=max(1, n // (workers * 4))))
        proc = time.perf_counter() - t
    return {"tasks": n, "prime_limit": limit, "threads": threads, "processes": workers,
            "serial_s": round(serial, 3), "threads_s": round(thr, 3), "asyncio_s": round(asy, 3),
            "process_pool_s": round(proc, 3), "process_speedup": round(serial / proc, 2),
            "thread_speedup": round(serial / thr, 2)}


# --------------------------------------------------------------------------- the app.py question


async def _to_thread_probe(fn, n: int) -> dict:
    hb = Heartbeat()
    hb.start()
    await asyncio.sleep(0.05)
    t = time.perf_counter()
    await asyncio.gather(*(asyncio.to_thread(fn) for _ in range(n)))
    wall = time.perf_counter() - t
    s = await hb.stop()
    s["wall_s"] = round(wall, 3)
    return s


def to_thread_check(n: int = 40) -> dict:
    """Does `await asyncio.to_thread(agent.handle, ...)` (sysdesign/app.py) stall the loop?
    Measures the loop heartbeat while n agent turns run in threads, then while n CPU-heavy jobs do."""
    from sysdesign.app import build_app
    from sysdesign.auth import Identity

    agent = build_app().state.agent
    who = Identity("u-probe", "retail", "standard", frozenset({"chat"}))
    agent.rate_check = False
    q = "what is your refund policy for damaged items?"
    t = time.perf_counter()
    for _ in range(20):
        agent.handle(who, q)
    per_turn_ms = (time.perf_counter() - t) / 20 * 1e3
    t = time.perf_counter()
    for _ in range(20):
        image_preprocess(1)
    per_img_ms = (time.perf_counter() - t) / 20 * 1e3
    agent_stats = asyncio.run(_to_thread_probe(lambda: agent.handle(who, q), n))
    cpu_stats = asyncio.run(_to_thread_probe(lambda: image_preprocess(1), n))
    return {"agent_turn_ms": round(per_turn_ms, 2), "image_job_ms": round(per_img_ms, 2),
            "switch_interval_ms": sys.getswitchinterval() * 1e3,
            "loop_while_agent_turns_in_threads": agent_stats, "loop_while_cpu_jobs_in_threads": cpu_stats}


def results(quick: bool = False) -> dict:
    idle = 300 if quick else 2000
    return {"note": "measured on this machine; real sockets, real processes", "machine": machine(),
            "replay_inline": replay(idle=idle, mode="inline"), "replay_process_pool": replay(idle=idle, mode="process"),
            "io_experiment": io_experiment(), "cpu_experiment": cpu_experiment(), "to_thread_check": to_thread_check()}
