import random

import pytest

from sysdesign.clock import FakeClock
from sysdesign.providers import FakeProvider, cost_usd
from sysdesign.resilience import (
    CLOSED,
    HALF_OPEN,
    OPEN,
    CircuitBreaker,
    ModelGateway,
    RetryPolicy,
    Unavailable,
    backoff_delays,
)


def gw(clock, primary=None, fallback=None, **kw):
    p = primary or FakeProvider("primary")
    f = fallback or FakeProvider("fallback", seed=3)
    br = {x.name: CircuitBreaker(failure_threshold=3, window_s=10, cooldown_s=5, clock=clock) for x in (p, f)}
    return ModelGateway([p, f], breakers=br, retry=RetryPolicy(max_retries=2), clock=clock, **kw), p, f


def test_breaker_full_cycle():
    c = FakeClock(0)
    b = CircuitBreaker(failure_threshold=2, window_s=10, cooldown_s=5, clock=c)
    b.record_failure()
    assert b.state == CLOSED
    b.record_failure()
    assert b.state == OPEN and not b.allow()
    c.advance(5)
    assert b.allow() and b.state == HALF_OPEN
    assert not b.allow()  # only one probe in half-open
    b.record_success()
    assert b.state == CLOSED


def test_half_open_failure_reopens():
    c = FakeClock(0)
    b = CircuitBreaker(failure_threshold=1, cooldown_s=5, clock=c)
    b.record_failure()
    c.advance(5)
    assert b.allow()
    b.record_failure()
    assert b.state == OPEN and not b.allow()


def test_failures_outside_window_do_not_trip():
    c = FakeClock(0)
    b = CircuitBreaker(failure_threshold=3, window_s=10, clock=c)
    for _ in range(5):
        b.record_failure()
        c.advance(6)
    assert b.state == CLOSED


def test_backoff_is_bounded_and_jittered():
    d = backoff_delays(6, 0.2, 2.0, random.Random(1))
    assert all(0 <= x <= 2.0 for x in d) and len(set(d)) == 6
    for i, x in enumerate(d):
        assert x <= min(2.0, 0.2 * 2 ** i)


def test_fallback_on_outage():
    c = FakeClock(0)
    g, p, f = gw(c)
    p.outage = True
    out = g.complete("small", "q", answer="ok")
    assert out.provider == "fallback" and g.stats["fallbacks"] == 1


def test_retries_limited_then_fallback():
    c = FakeClock(0)
    g, p, f = gw(c)
    p.outage = True
    g.complete("small", "q")
    assert p.calls == 3  # 1 + 2 retries, never unbounded


def test_open_breaker_skips_primary_entirely():
    c = FakeClock(0)
    g, p, f = gw(c)
    p.outage = True
    g.complete("small", "q")  # trips after 3 failures
    before = p.calls
    g.complete("small", "q")
    assert p.calls == before and g.stats["short_circuits"] >= 1


def test_all_down_raises_unavailable_not_a_guess():
    c = FakeClock(0)
    g, p, f = gw(c)
    p.outage = f.outage = True
    with pytest.raises(Unavailable):
        g.complete("small", "q")


def test_fallback_disabled_fails():
    c = FakeClock(0)
    g, p, f = gw(c, fallback_enabled=False)
    p.outage = True
    with pytest.raises(Unavailable):
        g.complete("small", "q")
    assert f.calls == 0


def test_timeout_counts_and_falls_back():
    c = FakeClock(0)
    slow = FakeProvider("primary", latency_s={"small": 50.0, "large": 50.0})
    g, p, f = gw(c, primary=slow)
    out = g.complete("small", "q")
    assert out.provider == "fallback" and g.stats["timeouts"] >= 1


def test_rate_limited_provider_uses_fallback():
    c = FakeClock(0)
    g, p, f = gw(c)
    p.rate_limited = True
    assert g.complete("small", "q").provider == "fallback"


def test_cost_rejects_negative_tokens():
    with pytest.raises(ValueError):
        cost_usd("small", -10, 5)
    assert cost_usd("large", 1_000_000, 0) == 2.5
