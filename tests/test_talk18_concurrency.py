"""Talk 18 (C): I/O-bound vs CPU-bound on one event loop. Small sizes and loose bounds so CI cannot flake."""
import asyncio
import json
import os
import pathlib
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from sysdesign.ext.concurrency import (
    Heartbeat,
    count_primes,
    cpu_experiment,
    image_preprocess,
    io_experiment,
    machine,
    replay,
    to_thread_check,
)

ROOT = pathlib.Path(__file__).resolve().parent.parent
MANY_CPUS = (os.cpu_count() or 1) >= 2


@pytest.mark.parametrize("limit,n", [(10, 4), (100, 25), (1000, 168)])
def test_count_primes(limit, n):
    assert count_primes(limit) == n


def test_image_preprocess_is_deterministic_and_seed_sensitive():
    assert image_preprocess(3, 64, 48) == image_preprocess(3, 64, 48) != image_preprocess(4, 64, 48)


def test_machine_reports_cpu_and_python():
    m = machine()
    assert m["cpu_count"] >= 1 and m["python"].count(".") == 2 and isinstance(m["gil_enabled"], bool)


def test_heartbeat_sees_a_blocking_call():
    async def run():
        hb = Heartbeat()
        hb.start()
        await asyncio.sleep(0.02)
        time.sleep(0.15)  # blocks the loop
        await asyncio.sleep(0.02)
        return await hb.stop()
    assert asyncio.run(run())["max_stall_ms"] >= 120


def test_heartbeat_quiet_when_loop_free():
    async def run():
        hb = Heartbeat()
        hb.start()
        await asyncio.sleep(0.1)
        return await hb.stop()
    assert asyncio.run(run())["beats"] >= 5


def test_awaiting_a_plain_value_is_a_type_error():
    async def bad():
        return await count_primes(10)  # the mistake: await on an int
    with pytest.raises(TypeError):
        asyncio.run(bad())


def test_async_def_without_await_never_yields():
    order = []

    async def cpu(i):
        count_primes(2000)
        order.append(i)

    async def tick():
        order.append("tick")

    async def main():
        await asyncio.gather(cpu(1), cpu(2), tick(), cpu(3))
    asyncio.run(main())
    assert order == [1, 2, "tick", 3]  # runs strictly in order: no interleaving without an await


@pytest.fixture(scope="module")
def inline():
    return replay(idle=200, jobs=20, mode="inline")


@pytest.fixture(scope="module")
def pooled():
    return replay(idle=200, jobs=20, mode="process", workers=2)


def test_idle_connections_keep_loop_responsive(inline):
    assert inline["idle_loop"]["max_stall_ms"] < 100


def test_cpu_on_loop_stalls_heartbeat(inline):
    one = image_preprocess(0)  # warm
    t = time.perf_counter()
    image_preprocess(1)
    per_job = time.perf_counter() - t
    assert inline["during_uploads"]["max_stall_ms"] >= 0.5 * 20 * per_job * 1e3
    assert one is not None


def test_process_pool_keeps_loop_responsive(inline, pooled):
    assert pooled["during_uploads"]["max_stall_ms"] < inline["during_uploads"]["max_stall_ms"] / 3


def test_chat_ping_answers_during_pooled_uploads(pooled):
    assert pooled["chat_ping_ms"]["count"] >= 2


def test_replay_reports_throughput(inline, pooled):
    for r in (inline, pooled):
        assert r["throughput_jobs_per_s"] > 0 and r["jobs"] == 20 and r["idle_connections"] == 200


def test_io_experiment_asyncio_beats_thread_pool():
    r = io_experiment(n=600, sleep_s=0.02, threads=32)
    assert r["asyncio_s"] < r["thread_pool_s"] and r["thread_pool_s"] >= 0.9 * r["ideal_thread_s"]


@pytest.fixture(scope="module")
def cpu():
    return cpu_experiment(n=24, limit=6000, threads=8, workers=2)


def test_threads_give_no_real_speedup_for_cpu_work(cpu):
    if not machine()["gil_enabled"]:
        pytest.skip("free-threaded build: threads can run CPU work in parallel")
    assert cpu["thread_speedup"] < 1.4


def test_asyncio_gives_no_speedup_for_cpu_work(cpu):
    assert cpu["asyncio_s"] > 0.7 * cpu["serial_s"]


@pytest.mark.skipif(not MANY_CPUS, reason="needs 2+ CPUs")
def test_process_pool_speeds_up_cpu_work(cpu):
    assert cpu["process_speedup"] > 1.2


def test_default_to_thread_pool_is_small():
    # asyncio.to_thread uses the loop's default executor: min(32, cpu + 4) threads
    with ThreadPoolExecutor() as ex:
        assert ex._max_workers == min(32, (os.cpu_count() or 1) + 4)


def test_to_thread_check_agent_vs_cpu():
    r = to_thread_check(n=8)
    assert r["agent_turn_ms"] > 0 and r["image_job_ms"] > 0
    assert r["loop_while_cpu_jobs_in_threads"]["max_stall_ms"] >= 0
    assert r["switch_interval_ms"] == pytest.approx(5.0, abs=50)


def test_committed_measurements_have_machine_and_all_sections():
    m = json.loads((ROOT / "docs/results/talk18_measured.json").read_text())
    for k in ("machine", "replay_inline", "replay_process_pool", "io_experiment", "cpu_experiment",
              "to_thread_check", "delay_proxy_rtt_100ms"):
        assert k in m
    assert m["io_experiment"]["tasks"] == 5000 and m["io_experiment"]["threads"] == 256
    assert m["cpu_experiment"]["tasks"] == 150


def test_committed_measurements_tell_the_story():
    m = json.loads((ROOT / "docs/results/talk18_measured.json").read_text())
    assert m["replay_inline"]["during_uploads"]["max_stall_ms"] > 100 * m["replay_process_pool"]["during_uploads"][
        "max_stall_ms"] / 10
    assert m["cpu_experiment"]["thread_speedup"] < 1.4 < m["cpu_experiment"]["process_speedup"]


def test_evidence_board_renders(tmp_path):
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    import talk18_evidence_board as b
    sim = json.loads((ROOT / "docs/results/talk18.json").read_text())
    meas = json.loads((ROOT / "docs/results/talk18_measured.json").read_text())
    b.draw(sim, meas, tmp_path / "a.png", tmp_path / "b.png")
    assert (tmp_path / "a.png").stat().st_size > 10_000 and (tmp_path / "b.png").stat().st_size > 5_000
