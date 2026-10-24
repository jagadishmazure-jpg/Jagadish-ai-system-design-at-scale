import math

import pytest

from sysdesign.autoscaler import Autoscaler
from sysdesign.capacity import Inputs, compute
from sysdesign.observability import functional_eval, make_tracer, percentile, slo_alerts


def test_scales_on_depth_with_startup_delay():
    a = Autoscaler(warm_min=1, max_workers=10, jobs_per_worker=10, startup_s=5)
    assert a.step(0, depth=55, oldest_age_s=1) == 1  # new workers not ready yet
    assert a.step(5, depth=55, oldest_age_s=1) == 6


def test_scales_on_oldest_age_even_when_depth_small():
    a = Autoscaler(warm_min=1, jobs_per_worker=100, max_oldest_age_s=20, startup_s=0)
    a.step(0, depth=3, oldest_age_s=45)
    assert a.step(0, depth=3, oldest_age_s=45) >= 2


def test_warm_minimum_and_cap():
    a = Autoscaler(warm_min=2, max_workers=4, startup_s=0, scale_down_cooldown_s=0)
    assert a.step(0, 0, 0) == 2
    a.step(1, 10_000, 0)
    assert a.step(2, 10_000, 0) == 4
    assert a.step(100, 0, 0) == 2  # never below warm minimum


def test_scale_down_waits_for_cooldown():
    a = Autoscaler(warm_min=1, startup_s=0, scale_down_cooldown_s=60, jobs_per_worker=10)
    a.step(0, 50, 0)
    a.step(0, 50, 0)
    assert a.step(30, 0, 0) == 5
    assert a.step(61, 0, 0) == 1


def test_cpu_is_not_an_input():
    import inspect
    assert "cpu" not in inspect.signature(Autoscaler.step).parameters


def test_autoscaler_validates_config():
    with pytest.raises(ValueError):
        Autoscaler(warm_min=5, max_workers=2)
    with pytest.raises(ValueError):
        Autoscaler(jobs_per_worker=0)


def test_percentile_nearest_rank_and_empty_is_nan():
    assert percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 95) == 10
    assert percentile([3.0], 50) == 3.0
    assert math.isnan(percentile([], 95))


def test_slo_alerts_fire():
    alerts = slo_alerts([3.5] * 30, cost_usd=20, tokens=10_000_000, window_h=1)
    assert {a["slo"] for a in alerts} == {"p95_latency", "llm_spend_per_hour", "tokens_per_hour"}


def test_slo_quiet_when_healthy():
    assert slo_alerts([0.5] * 100, cost_usd=1, tokens=1000, window_h=1) == []


def test_slo_does_not_pass_on_tiny_sample():
    # 3 fast samples must not count as "p95 met"
    assert slo_alerts([0.1] * 3, 0, 0, 1)[0]["slo"] == "insufficient_samples"


def test_functional_eval():
    assert functional_eval("order_status", ["order_status"])["pass"]
    assert not functional_eval("order_status", [])["pass"]
    assert not functional_eval("escalate", ["issue_refund"])["pass"]
    assert not functional_eval("made_up_intent", ["issue_refund"])["pass"]


def test_traces_are_redacted_at_export():
    tracer, exp = make_tracer()
    with tracer.start_as_current_span("x") as s:
        s.set_attribute("msg", "card 4111111111111111 mail a@b.co")
    assert exp.spans[0]["attributes"]["msg"] == "card [CARD] mail [EMAIL]"


def test_capacity_math_is_computed():
    out = compute(Inputs(users=10_000, requests_per_user_per_day=5, peak_multiplier=10))
    assert out["requests_per_day"] == 50_000
    assert out["avg_rps"] == pytest.approx(50_000 / 86_400, abs=1e-3)
    assert out["peak_rps"] == pytest.approx(10 * 50_000 / 86_400, abs=1e-2)
    assert out["allowed_downtime_min_per_month"] == pytest.approx(43.2)
    assert out["llm_cost_per_month_usd"] < out["all_large_model_cost_per_month_usd"]


def test_capacity_rejects_bad_inputs():
    with pytest.raises(ValueError):
        compute(Inputs(share_small=0.9))
    with pytest.raises(ValueError):
        compute(Inputs(users=0))
