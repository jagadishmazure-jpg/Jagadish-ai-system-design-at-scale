"""Talk 17 add-on: retry budget, in-flight duplicates, amplification. Virtual time, seeded."""
import asyncio

import fakeredis
import pytest

from sysdesign.clock import FakeClock
from sysdesign.ext import retries as rt
from sysdesign.jobqueue import JobQueue
from sysdesign.resilience import RetryPolicy
from sysdesign.tools import Commerce


def fill(budget, clock, n=400, gap=0.15):
    for i in range(n):
        clock.t = 1_000_000.0 + i * gap
        budget.record_request()


def test_budget_is_10pct_of_400_per_minute():
    c = FakeClock()
    b = rt.RetryBudget(0.10, clock=c)
    fill(b, c)
    assert b.allowance() == 40


def test_budget_spends_then_denies():
    c = FakeClock()
    b = rt.RetryBudget(0.10, clock=c)
    fill(b, c)
    assert sum(b.try_spend() for _ in range(50)) == 40 and b.denied == 10


def test_budget_window_slides():
    c = FakeClock()
    b = rt.RetryBudget(0.10, clock=c)
    fill(b, c)
    for _ in range(40):
        b.try_spend()
    c.t += 120
    assert b.allowance() == 0


def test_budget_rejects_negative_inputs():
    for kw in ({"ratio": -0.1}, {"window_s": 0}, {"min_per_window": -1}):
        with pytest.raises(ValueError):
            rt.RetryBudget(**kw)


def test_min_per_window_lets_quiet_services_retry():
    b = rt.RetryBudget(0.10, min_per_window=2, clock=FakeClock())
    assert b.try_spend() and b.try_spend() and not b.try_spend()


def failing(n_fail):
    state = {"n": 0}

    def fn():
        state["n"] += 1
        if state["n"] <= n_fail:
            raise RuntimeError("down")
        return "ok"
    return fn, state


def test_retry_succeeds_within_policy():
    fn, s = failing(2)
    assert rt.call_with_budget(fn, RetryPolicy(max_retries=2), None) == "ok" and s["n"] == 3


def test_retry_gives_up_after_policy():
    fn, s = failing(5)
    with pytest.raises(rt.Unavailable):
        rt.call_with_budget(fn, RetryPolicy(max_retries=2), None)
    assert s["n"] == 3


def test_empty_budget_fails_fast_with_503():
    fn, s = failing(5)
    b = rt.RetryBudget(0.10, clock=FakeClock())  # 1 request seen, 0 retries allowed
    assert rt.http_status(fn, RetryPolicy(max_retries=2), b) == 503 and s["n"] == 1


def test_healthy_call_is_200_and_spends_nothing():
    fn, _ = failing(0)
    b = rt.RetryBudget(0.10, clock=FakeClock())
    assert rt.http_status(fn, RetryPolicy(), b) == 200 and b.denied == 0


def test_error_message_has_no_payload():
    fn, _ = failing(5)
    with pytest.raises(rt.Unavailable) as e:
        rt.call_with_budget(fn, RetryPolicy(max_retries=0), None)
    assert "@" not in str(e.value)


# ---------------------------------------------------------------- amplification
def test_outage_one_layer_is_3x():
    assert rt.amplification(1.0, retry_layers="one")["calls_per_click"] == 3.0


def test_outage_every_layer_is_27x():
    assert rt.amplification(1.0, retry_layers="all")["calls_per_click"] == 27.0


def test_outage_one_layer_with_budget_is_about_1_1x():
    assert rt.amplification(1.0, retry_layers="one", budget_ratio=0.10)["calls_per_click"] == pytest.approx(1.1, abs=0.02)


def test_budget_in_every_layer_still_compounds():
    v = rt.amplification(1.0, retry_layers="all", budget_ratio=0.10)["calls_per_click"]
    assert 1.1 < v < 1.5


def test_healthy_traffic_barely_amplifies():
    assert rt.amplification(0.05, retry_layers="all")["calls_per_click"] < 1.1


def test_worst_case_formula():
    assert rt.amplification(1.0, retry_layers="all", clicks=10)["worst_case_calls"] == 27


def test_quoted_amplification_is_labelled():
    assert "quoted" in rt.QUOTED_AMPLIFICATION_WITH_BUDGET["source"]


def test_results_deterministic():
    assert rt.results() == rt.results()


# ---------------------------------------------------------------- in-flight duplicates
@pytest.fixture
def qs():
    return JobQueue(fakeredis.FakeRedis(), clock=FakeClock()), Commerce()


def test_second_click_waits_and_shares_result(qs):
    q, shop = qs
    out = asyncio.run(rt.double_click(q, shop))
    assert out["first"]["ran"] and not out["second"]["ran"]
    assert out["first"]["result"] == out["second"]["result"]


def test_side_effect_runs_once(qs):
    q, shop = qs
    out = asyncio.run(rt.double_click(q, shop))
    assert out["refunds_issued"] == 1 and out["issue_refund_calls"] == 1


def test_second_click_really_arrived_in_flight(qs):
    q, shop = qs
    out = asyncio.run(rt.double_click(q, shop, gap_s=0.01, work_s=0.05))
    assert out["second"]["polls"] > 0 and out["first"]["job_id"] == out["second"]["job_id"]


def test_late_click_reads_stored_result(qs):
    q, shop = qs
    first = asyncio.run(rt.click(q, shop, "u-ava", "KG-100002", 10.0, "late-click-0001", work_s=0.0))
    again = asyncio.run(rt.click(q, shop, "u-ava", "KG-100002", 10.0, "late-click-0001"))
    assert not again["ran"] and again["result"] == first["result"] and len(shop.refunds) == 1


def test_different_keys_run_separately(qs):
    q, shop = qs
    asyncio.run(rt.click(q, shop, "u-ava", "KG-100002", 5.0, "key-aaaa-0001", work_s=0.0))
    asyncio.run(rt.click(q, shop, "u-ava", "KG-100002", 5.0, "key-bbbb-0002", work_s=0.0))
    assert len(shop.refunds) == 2
