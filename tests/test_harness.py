import asyncio

from loadtest import faults, harness


def test_fault_scenarios_all_safe():
    r = asyncio.run(faults.main())
    assert r["all_safe"], r


def test_patterns_move_numbers_in_the_right_direction():
    sc = harness.SCALES["ci"]
    off, on = asyncio.run(harness.cache_test(False, sc)), asyncio.run(harness.cache_test(True, sc))
    assert on["hit_rate"] > 0.5 and on["model_calls"] < off["model_calls"] and on["cost_usd"] < off["cost_usd"]
    r_off, r_on = asyncio.run(harness.router_test(False, sc)), asyncio.run(harness.router_test(True, sc))
    assert r_on["cost_usd"] < r_off["cost_usd"]
    o_off, o_on = asyncio.run(harness.outage_test(False, sc)), asyncio.run(harness.outage_test(True, sc))
    assert o_off["error_rate"] > 0.2 and o_on["error_rate"] == 0.0 and o_on["fallbacks_used"] > 0
    q_off, q_on = asyncio.run(harness.queue_drain(False, sc)), asyncio.run(harness.queue_drain(True, sc))
    assert q_on["drain_s"] < q_off["drain_s"]
    n_on = asyncio.run(harness.noisy_neighbor(True, sc))
    assert n_on["abuser_429_rate"] > 0.5
