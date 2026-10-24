import pytest

from sysdesign.admission import Admission, Limits


def lim(**kw):
    return {"standard": Limits(**kw)}


def test_burst_then_429_with_retry_after(r, clock):
    a = Admission(r, clock=clock, limits=lim(rate_per_s=1, burst=3))
    assert [a.check_rate("t", "u", "standard").allowed for _ in range(3)] == [True] * 3
    d = a.check_rate("t", "u", "standard")
    assert not d.allowed and d.reason == "rate" and d.retry_after_s >= 1


def test_bucket_refills_over_time(r, clock):
    a = Admission(r, clock=clock, limits=lim(rate_per_s=1, burst=1))
    assert a.check_rate("t", "u", "standard").allowed
    assert not a.check_rate("t", "u", "standard").allowed
    clock.advance(1.01)
    assert a.check_rate("t", "u", "standard").allowed


def test_limits_shared_across_replicas(r, clock):
    # two Admission objects = two replicas pointing at the same Redis
    a1 = Admission(r, clock=clock, limits=lim(rate_per_s=0.001, burst=4))
    a2 = Admission(r, clock=clock, limits=lim(rate_per_s=0.001, burst=4))
    results = [(a1 if i % 2 else a2).check_rate("t", "u", "standard").allowed for i in range(8)]
    assert results.count(True) == 4


def test_users_and_tenants_isolated(r, clock):
    a = Admission(r, clock=clock, limits=lim(rate_per_s=0.001, burst=1))
    assert a.check_rate("t1", "u", "standard").allowed
    assert a.check_rate("t2", "u", "standard").allowed
    assert a.check_rate("t1", "v", "standard").allowed


def test_unknown_tier_gets_standard_not_more(r, clock):
    a = Admission(r, clock=clock, limits={"standard": Limits(burst=1, rate_per_s=0.001),
                                          "premium": Limits(burst=100)})
    assert a.limits_for("platinum-hacker").burst == 1


def test_daily_budget_enforced(r, clock):
    a = Admission(r, clock=clock, limits=lim(tokens_per_day=1000, tokens_per_month=10_000))
    assert a.reserve_tokens("t", "u", "standard", 600).allowed
    d = a.reserve_tokens("t", "u", "standard", 600)
    assert not d.allowed and d.reason == "budget_day" and d.retry_after_s > 0


def test_monthly_budget_enforced(r, clock):
    a = Admission(r, clock=clock, limits=lim(tokens_per_day=10_000, tokens_per_month=1000))
    assert a.reserve_tokens("t", "u", "standard", 900).allowed
    assert a.reserve_tokens("t", "u", "standard", 200).reason == "budget_month"


def test_negative_estimate_cannot_refill_budget(r, clock):
    a = Admission(r, clock=clock, limits=lim(tokens_per_day=1000))
    with pytest.raises(ValueError):
        a.reserve_tokens("t", "u", "standard", -5000)
    with pytest.raises(ValueError):
        a.settle_tokens("t", "u", 10, -1)


def test_settle_charges_actual_usage_once(r, clock):
    a = Admission(r, clock=clock)
    a.reserve_tokens("t", "u", "standard", 500)
    a.settle_tokens("t", "u", 500, 320)
    assert a.used("t", "u") == (320, 320)


def test_rejected_reservation_does_not_charge(r, clock):
    a = Admission(r, clock=clock, limits=lim(tokens_per_day=100))
    a.reserve_tokens("t", "u", "standard", 150)
    assert a.used("t", "u") == (0, 0)


def test_model_concurrency_cap_and_lease_expiry(r, clock):
    a = Admission(r, clock=clock, limits=lim(max_concurrent_model_calls=2, lease_s=5))
    h1, h2 = a.acquire_model_slot(), a.acquire_model_slot()
    assert h1 and h2 and a.acquire_model_slot() is None
    a.release_model_slot(h1)
    assert a.acquire_model_slot() is not None
    clock.advance(6)  # a crashed replica's leases expire
    assert a.acquire_model_slot() is not None


def test_month_budget_retry_after_points_to_next_month(r):
    from sysdesign.clock import FakeClock
    c = FakeClock(1_798_761_600.0 - 3600)  # 2026-12-31 23:00 UTC
    a = Admission(r, clock=c, limits=lim(tokens_per_day=10**9, tokens_per_month=10))
    d = a.reserve_tokens("t", "u", "standard", 11)
    assert d.reason == "budget_month" and d.retry_after_s == 3600
