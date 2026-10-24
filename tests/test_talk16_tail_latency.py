"""Talk 16: tail latency and resilience. All simulated; see docs/tail-latency.md."""
import json
import math
import pathlib
import random

import pytest

from sysdesign.clock import FakeClock
from sysdesign.ext import tail_latency as tl
from sysdesign.resilience import CLOSED, HALF_OPEN, OPEN, CircuitBreaker, RetryPolicy, SlowCallBreaker

ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def inc():
    return tl.replay_incident(8000)


@pytest.fixture(scope="module")
def bo():
    return {"one": tl.brownout(), "two": tl.brownout(replicas=2), "fixed": tl.brownout(fixed=True),
            "bulkhead_only": tl.brownout(fixed=True, use_breaker=False)}


# ---- incident setup
def test_six_sequential_calls():
    assert [d.name for d in tl.DEPS] == ["session", "order", "retrieval", "moderation_in", "model",
                                         "moderation_out"]


def test_p50_sums_to_about_2_8s():
    assert tl.p50_sum() == pytest.approx(2.8, abs=0.05)


def test_constants_match_story():
    assert (tl.RPM, tl.WORKERS_PER_REPLICA, tl.BASE_TIMEOUT_S, tl.BASE_RETRIES, tl.DEADLINE_S) == (
        400, 100, 30.0, 3, 20.0)


def test_moderation_p99_healthy_near_300ms(inc):
    assert inc["moderation_p99_s"]["healthy"] == pytest.approx(0.3, abs=0.03)


def test_moderation_p99_incident_near_4s(inc):
    assert 3.5 <= inc["moderation_p99_s"]["incident"] <= 5.5


def test_moderation_p50_unchanged_in_incident():
    rng1, rng2 = random.Random(1), random.Random(1)
    d = tl.BY_NAME["moderation_in"]
    a = sorted(tl.sample_latency(d, rng1, False) for _ in range(5000))
    b = sorted(tl.sample_latency(d, rng2, True) for _ in range(5000))
    assert abs(a[2500] - b[2500]) < 0.02


def test_incident_raises_turn_p99(inc):
    assert inc["incident"]["p99_s"] > 1.5 * inc["healthy"]["p99_s"]


def test_incident_leaves_p50_alone(inc):
    assert inc["incident"]["p50_s"] == pytest.approx(inc["healthy"]["p50_s"], rel=0.05)


def test_incident_causes_deadline_misses(inc):
    assert inc["healthy"]["deadline_miss_rate"] == 0
    assert inc["incident"]["deadline_miss_rate"] > 0


def test_no_queueing_at_400_rpm(inc):
    # 400/min x ~3s is about 20 in flight; 100 workers never fill in the 1%-slow replay
    assert inc["incident"]["max_wait_s"] == 0


# ---- fan-out
def test_fanout_formula():
    for p, n, want in [(0.01, 1, 0.01), (0.01, 10, 0.0956), (0.01, 100, 0.634), (0.0, 5, 0.0), (1.0, 3, 1.0)]:
        assert tl.fanout_probability(p, n) == pytest.approx(want, abs=1e-3)


def test_fanout_rejects_bad_input():
    for p, n in [(-0.1, 2), (1.1, 2), (0.1, -1)]:
        with pytest.raises(ValueError):
            tl.fanout_probability(p, n)


def test_fanout_formula_matches_simulation():
    for row in tl.fanout_curve():
        assert row["simulated"] == pytest.approx(row["formula"], abs=0.01)


def test_fanout_curve_increasing():
    f = [r["formula"] for r in tl.fanout_curve()]
    assert f == sorted(f)


# ---- parallel
def test_parallel_cuts_median():
    r = tl.parallel_vs_sequential(8000)
    assert r["par_p50"] < r["seq_p50"] * 0.9


def test_parallel_barely_moves_tail():
    r = tl.parallel_vs_sequential(8000)
    p50_gain = 1 - r["par_p50"] / r["seq_p50"]
    p99_gain = 1 - r["par_p99"] / r["seq_p99"]
    assert p99_gain < p50_gain
    assert r["par_p999"] > 0.9 * r["seq_p999"]


def test_parallel_only_still_misses_deadlines(inc):
    assert inc["incident_parallel_only"]["deadline_miss_rate"] > 0


# ---- Little's law and saturation
def test_littles_law():
    assert tl.littles_law_in_flight(400 / 60, 30) == pytest.approx(200)


def test_seconds_to_saturate():
    assert tl.seconds_to_saturate(100, 400 / 60) == pytest.approx(15)
    assert tl.seconds_to_saturate(200, 400 / 60) == pytest.approx(30)


def test_brownout_saturates_one_replica(bo):
    assert bo["one"]["peak_busy_workers"] == 100
    assert 10 <= bo["one"]["seconds_after_brownout"] <= 20


def test_second_replica_only_delays_saturation(bo):
    assert bo["two"]["peak_busy_workers"] == 200
    delay = bo["two"]["seconds_after_brownout"] - bo["one"]["seconds_after_brownout"]
    assert 10 <= delay <= 20
    assert bo["two"]["deadline_miss_rate"] > 0.5


def test_in_flight_need_exceeds_workers(bo):
    assert bo["one"]["littles_law_in_flight_30s"] > tl.WORKERS_PER_REPLICA
    assert bo["one"]["littles_law_in_flight_with_retries"] == pytest.approx(800)


def test_fixed_never_saturates(bo):
    assert bo["fixed"]["saturated_at_s"] is None
    assert bo["fixed"]["peak_busy_workers"] < 50
    assert bo["fixed"]["deadline_miss_rate"] == 0


def test_bulkhead_alone_protects_workers(bo):
    assert bo["bulkhead_only"]["saturated_at_s"] is None
    assert bo["bulkhead_only"]["bulkhead_rejected"] > 0


def test_breaker_opens_in_brownout(bo):
    assert bo["fixed"]["breaker_open_at_end"] is True


# ---- slow-call breaker
def _br(**kw):
    return SlowCallBreaker(clock=FakeClock(0.0), **kw)


def test_slow_breaker_trips_on_slow_successes():
    b = _br()
    for _ in range(20):
        assert b.allow()
        b.record_call(3.0, ok=True)
    assert b.state == OPEN and b.slow_trips == 1


def test_plain_breaker_ignores_slow_successes():
    b = CircuitBreaker(clock=FakeClock(0.0))
    for _ in range(20):
        b.allow()
        b.record_success()
    assert b.state == CLOSED


def test_slow_breaker_needs_full_window():
    b = _br()
    for _ in range(19):
        b.record_call(3.0, ok=True)
    assert b.state == CLOSED


def test_slow_breaker_half_rate_does_not_trip():
    b = _br()
    for i in range(20):
        b.record_call(3.0 if i % 2 else 0.1, ok=True)
    assert b.slow_rate() == 0.5 and b.state == CLOSED


def test_slow_breaker_keeps_failure_trip():
    b = _br(failure_threshold=5)
    for _ in range(5):
        b.record_call(0.1, ok=False)
    assert b.state == OPEN and b.failure_trips == 1 and b.slow_trips == 0


def test_slow_breaker_half_open_probe_closes_on_fast():
    b = _br(cooldown_s=10)
    for _ in range(20):
        b.record_call(3.0, ok=True)
    b.clock.advance(10)
    assert b.allow() and b.state == HALF_OPEN
    assert not b.allow()  # one probe at a time
    b.record_call(0.1, ok=True)
    assert b.state == CLOSED


def test_slow_breaker_half_open_slow_probe_reopens():
    b = _br(cooldown_s=10)
    for _ in range(20):
        b.record_call(3.0, ok=True)
    b.clock.advance(10)
    b.allow()
    b.record_call(3.0, ok=True)
    assert b.state == OPEN


def test_slow_breaker_blocks_while_open():
    b = _br(cooldown_s=10)
    for _ in range(20):
        b.record_call(3.0, ok=True)
    b.clock.advance(5)
    assert not b.allow()


# ---- bulkheads
def test_bulkhead_formula():
    assert tl.bulkhead_size(2, 400 / 60, 0.3, slack=3) == math.ceil(2 * 400 / 60 * 0.3 * 3)


def test_bulkhead_formula_minimum_one():
    assert tl.bulkhead_size(1, 0.01, 0.01, 1) == 1


@pytest.mark.parametrize("bad", [0, -1])
def test_bulkhead_formula_rejects_nonpositive(bad):
    with pytest.raises(ValueError):
        tl.bulkhead_size(bad, 1, 1)


def test_bulkhead_rejects_when_full():
    b = tl.Bulkhead("moderation", 2)
    assert b.try_acquire() and b.try_acquire() and not b.try_acquire()
    assert b.rejected == 1
    b.release()
    assert b.try_acquire()


def test_existing_job_bulkheads_still_there():
    from sysdesign.jobqueue import POOLS
    assert set(POOLS) == {"interactive", "bulk"}


# ---- hedging
def test_hedging_cuts_tail(inc):
    assert inc["incident_fixed"]["p99_s"] < 0.6 * inc["incident"]["p99_s"]


def test_hedge_extra_load_at_most_about_5pct(inc):
    assert 0 < inc["incident_fixed"]["hedge_extra_load"] <= 0.055


def test_writes_never_hedged():
    assert not tl.hedge_allowed(side_effect=True)
    assert not tl.hedge_allowed(idempotent=False)
    assert not tl.hedge_allowed(tl.Dep("create_refund", 0.2, 0.5, side_effect=True, idempotent=False))


def test_reads_hedged_model_not():
    assert tl.hedge_allowed(tl.BY_NAME["moderation_in"])
    assert not tl.hedge_allowed(tl.BY_NAME["model"])


def test_side_effect_call_never_duplicated():
    dep = tl.Dep("create_refund", 0.2, 0.5, side_effect=True, idempotent=False)
    stats = {}
    tl.call_dep(dep, random.Random(1), incident=False, timeout_s=30, retries=0, hedge_at=0.0, stats=stats)
    assert stats.get("hedges", 0) == 0 and stats["calls"] == 1


def test_hedge_thresholds_are_p95():
    h = tl.hedge_thresholds(False, n=5000)
    assert h["moderation_in"] == pytest.approx(0.15 * math.exp(1.645 * tl.BY_NAME["moderation_in"].sigma), rel=0.1)


# ---- deadline budget and timeouts
def test_slices_sum_to_deadline():
    assert sum(tl.SLICES.values()) == pytest.approx(20)
    assert tl.SLICES["model"] == 12


def test_bad_slices_rejected():
    with pytest.raises(ValueError):
        tl.DeadlineBudget({"model": 12})


def test_overrun_cannot_take_later_slices():
    b = tl.DeadlineBudget()
    used, ok = b.run("model", 30.0)
    assert used == 12 and not ok
    assert b.slices["moderation_out"] == 1.5  # untouched


def test_unused_time_goes_to_bank():
    b = tl.DeadlineBudget()
    b.run("session", 0.1)
    assert b.bank == pytest.approx(3.5 + 0.4)


def test_fallback_uses_bank_only():
    b = tl.DeadlineBudget()
    b.run("model", 30.0)
    used, ok = b.run_fallback("model", 1.2)
    assert ok and b.bank == pytest.approx(3.5 - 1.2)
    used, ok = b.run_fallback("model", 10)
    assert not ok and b.bank == 0


def test_timeout_derived_from_slice():
    b = tl.DeadlineBudget()
    assert b.timeout_for("parallel", attempts=2) == pytest.approx(1.25)
    assert b.timeout_for("model") == 12
    assert b.timeout_for("model") < tl.BASE_TIMEOUT_S


def test_old_fixed_timeout_still_default():
    assert RetryPolicy().timeout_s == 4.0


def test_fixed_policy_meets_deadline(inc):
    assert inc["incident_fixed"]["deadline_miss_rate"] == 0


# ---- fallback ladder
def test_ladder_order():
    assert tl.LADDER == ("primary", "secondary_provider", "small_local_model", "cached_answer", "honest_error")


@pytest.mark.parametrize("avail,cached,want", [
    ({"primary": True}, None, "primary"),
    ({"secondary_provider": True}, None, "secondary_provider"),
    ({"small_local_model": True}, None, "small_local_model"),
    ({}, "Refunds take 5 days.", "cached_answer"),
    ({}, None, "honest_error"),
])
def test_ladder_rungs(avail, cached, want):
    r = tl.model_ladder(avail, cached)
    assert r.rung == want
    assert r.degraded == (want != "primary")


def test_ladder_skips_rung_that_does_not_fit_budget():
    r = tl.model_ladder({"secondary_provider": True, "small_local_model": True},
                        latencies={"secondary_provider": 5.0, "small_local_model": 0.8}, bank_s=2.0)
    assert r.rung == "small_local_model"


def test_side_effects_never_degrade():
    r = tl.model_ladder({"secondary_provider": True, "small_local_model": True}, cached="x", side_effect=True)
    assert r.rung == "honest_error" and r.tried == ["primary", "honest_error"]


def test_honest_error_is_honest():
    assert "can't answer" in tl.HONEST_ERROR


def test_fresh_session_fallback():
    s = tl.load_session(False, ["hi"])
    assert s["history"] == [] and s["notice"] == tl.FRESH_SESSION_NOTICE and s["degraded"]
    assert tl.load_session(True, ["hi"])["history"] == ["hi"]


def test_retrieval_fallback_has_disclaimer():
    a = tl.answer_with_retrieval(False)
    assert not a["grounded"] and a["disclaimer"] == tl.NO_RETRIEVAL_DISCLAIMER
    assert tl.answer_with_retrieval(True, ["doc"])["disclaimer"] is None


def test_agent_degraded_reply_still_exists():
    from sysdesign.agent import DEGRADED
    assert "can't answer" in DEGRADED


# ---- determinism, committed results and board
def test_simulation_deterministic():
    assert tl.simulate(incident=True, turns=500) == tl.simulate(incident=True, turns=500)


def test_committed_results_match_fresh_run():
    committed = json.loads((ROOT / "docs/results/tail_latency.json").read_text())
    assert committed == json.loads(json.dumps(tl.results()))


def test_board_json_present():
    n = json.loads((ROOT / "docs/results/tail_evidence_board.json").read_text())
    assert set(n) >= {"a_fanout", "b_turns", "c_busy", "d_slices"}
    assert (ROOT / "docs/img/tail_evidence_board.png").stat().st_size > 10_000
