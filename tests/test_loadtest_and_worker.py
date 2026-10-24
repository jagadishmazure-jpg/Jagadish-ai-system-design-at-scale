import itertools
import json
import pathlib

import pytest

from loadtest.harness import OUT
from sysdesign.autoscaler import Autoscaler
from sysdesign.jobqueue import JobQueue
from sysdesign.worker import autoscale_tick, desired_key

RES = json.loads(pathlib.Path(OUT).read_text())


def test_rate_limiting_protects_normal_users():
    off, on = RES["rate_limiting"]["off"], RES["rate_limiting"]["on"]
    assert on["normal_users"]["p95_s"] < off["normal_users"]["p95_s"]
    assert on["normal_users"]["error_rate"] <= off["normal_users"]["error_rate"]
    assert on["normal_users"]["rate_429"] == 0.0 and on["abuser"]["rate_429"] > 0.5


def test_cache_saves_cost_without_wrong_answers():
    off, on = RES["semantic_cache"]["off"], RES["semantic_cache"]["on"]
    assert on["cost_usd"] < off["cost_usd"] and on["cache_hit_rate"] > 0
    assert on["wrong_source_hits"] == 0 and off["cache_hit_rate"] == 0.0


def test_router_ladder_cuts_cost():
    r = RES["intent_router"]
    assert r["on_ladder"]["cost_usd"] < r["off_all_large_model"]["cost_usd"]
    assert r["on_ladder"]["paths"]["rules"]["cost_usd"] == 0.0


def test_breaker_and_fallback_remove_outage_errors():
    o = RES["provider_outage"]
    assert o["on_breaker_fallback"]["outage_window"]["error_rate"] < o["off_naive_retries"]["outage_window"]["error_rate"]
    assert o["on_breaker_fallback"]["primary_calls"] < o["off_naive_retries"]["primary_calls"]  # no retry storm


def test_health_checks_cut_replica_failure_errors():
    f = RES["replica_failure"]
    assert f["on_health_checked_lb"]["error_rate"] < f["off_no_health_checks"]["error_rate"]


def test_bulkheads_and_autoscaling_protect_interactive_jobs():
    q = RES["queue_and_autoscaling"]
    assert q["on_bulkheads_autoscaled"]["interactive_wait_p95_s"] < q["off_shared_fixed_2"]["interactive_wait_p95_s"]
    assert q["on_bulkheads_autoscaled"]["bulk_drain_time_s"] < q["off_shared_fixed_2"]["bulk_drain_time_s"]


def test_request_counts_match_between_modes():
    # off and on must replay identical traffic, otherwise the comparison is rigged
    for k in ("semantic_cache", "intent_router", "replica_failure", "provider_outage"):
        assert len({v["requests"] for v in RES[k].values()}) == 1


@pytest.mark.parametrize("scenario", ["semantic_cache"])
def test_harness_is_deterministic(scenario):
    from loadtest import harness
    a, b = harness.scenario_cache(), harness.scenario_cache()
    assert a == b == RES[scenario]


def test_autoscale_tick_publishes_desired(r, clock):
    ids = itertools.count()
    q = JobQueue(r, clock=clock, id_factory=lambda: f"j{next(ids)}")
    for _ in range(45):
        q.enqueue("bulk", {})
    sc = {"bulk": Autoscaler(warm_min=1, jobs_per_worker=10, startup_s=0)}
    assert autoscale_tick(r, q, sc, clock.now()) == {"bulk": 1}  # workers requested, not yet started
    assert autoscale_tick(r, q, sc, clock.now()) == {"bulk": 5}
    assert int(r.get(desired_key("bulk"))) == 5
