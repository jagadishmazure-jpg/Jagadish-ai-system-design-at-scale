"""Timeouts, bounded retries with exponential backoff + full jitter, a circuit breaker,
and a model gateway that fails over from the primary provider to a fallback.

Graceful degradation: when every provider is unavailable the gateway raises
Unavailable; the agent then answers with an explicit "can't answer right now"
message and never invents an answer.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from .clock import RealClock
from .providers import Completion, ProviderError

CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"


class Unavailable(Exception):
    pass


class CircuitOpen(Exception):
    pass


@dataclass
class CircuitBreaker:
    failure_threshold: int = 5
    window_s: float = 30.0
    cooldown_s: float = 10.0
    half_open_max_calls: int = 1
    clock: object = field(default_factory=RealClock)
    state: str = CLOSED
    transitions: list = field(default_factory=list)

    def __post_init__(self) -> None:
        self._failures: list[float] = []
        self._opened_at = 0.0
        self._half_open_in_flight = 0

    def _set(self, state: str) -> None:
        if state != self.state:
            self.transitions.append((round(self.clock.now(), 3), self.state, state))
            self.state = state

    def allow(self) -> bool:
        now = self.clock.now()
        if self.state == OPEN and now - self._opened_at >= self.cooldown_s:
            self._set(HALF_OPEN)
            self._half_open_in_flight = 0
        if self.state == OPEN:
            return False
        if self.state == HALF_OPEN:
            if self._half_open_in_flight >= self.half_open_max_calls:
                return False
            self._half_open_in_flight += 1
        return True

    def record_success(self) -> None:
        if self.state == HALF_OPEN:
            self._set(CLOSED)
            self._half_open_in_flight = 0
        self._failures.clear()

    def record_failure(self) -> None:
        now = self.clock.now()
        if self.state == HALF_OPEN:
            self._open(now)
            return
        self._failures = [t for t in self._failures if now - t < self.window_s] + [now]
        if len(self._failures) >= self.failure_threshold:
            self._open(now)

    def _open(self, now: float) -> None:
        self._set(OPEN)
        self._opened_at = now
        self._failures.clear()
        self._half_open_in_flight = 0


def backoff_delays(retries: int, base_s: float, cap_s: float, rng: random.Random) -> list[float]:
    """Full-jitter exponential backoff: uniform(0, min(cap, base * 2**attempt))."""
    return [rng.uniform(0, min(cap_s, base_s * (2 ** i))) for i in range(retries)]


@dataclass
class RetryPolicy:
    max_retries: int = 2
    base_s: float = 0.2
    cap_s: float = 2.0
    timeout_s: float = 4.0
    seed: int = 11


class ModelGateway:
    """Outbound gateway for model calls (contrast with the inbound API gateway)."""

    def __init__(self, providers: list, breakers: dict | None = None, retry: RetryPolicy | None = None,
                 clock=None, retries_enabled: bool = True, breaker_enabled: bool = True,
                 fallback_enabled: bool = True) -> None:
        self.providers = providers
        self.clock = clock or RealClock()
        self.breakers = breakers or {p.name: CircuitBreaker(clock=self.clock) for p in providers}
        self.retry = retry or RetryPolicy()
        self._rng = random.Random(self.retry.seed)
        self.retries_enabled = retries_enabled
        self.breaker_enabled = breaker_enabled
        self.fallback_enabled = fallback_enabled
        self.stats = {"attempts": 0, "retries": 0, "fallbacks": 0, "short_circuits": 0, "timeouts": 0,
                      "unavailable": 0}
        self.elapsed_s = 0.0  # time spent in the last call, including backoff waits

    def complete(self, tier: str, prompt: str, **kw) -> Completion:
        start = self.clock.now()
        chain = self.providers if self.fallback_enabled else self.providers[:1]
        for i, p in enumerate(chain):
            br = self.breakers[p.name]
            if self.breaker_enabled and not br.allow():
                self.stats["short_circuits"] += 1
                continue
            if i > 0:
                self.stats["fallbacks"] += 1
            retries = self.retry.max_retries if self.retries_enabled else 0
            delays = backoff_delays(retries, self.retry.base_s, self.retry.cap_s, self._rng)
            for attempt in range(retries + 1):
                self.stats["attempts"] += 1
                try:
                    c = p.complete(tier, prompt, **kw)
                    if c.latency_s > self.retry.timeout_s:
                        self.clock.sleep(self.retry.timeout_s)
                        self.stats["timeouts"] += 1
                        raise ProviderError("timeout", retryable=True, status=504)
                    self.clock.sleep(c.latency_s)
                    if self.breaker_enabled:
                        br.record_success()
                    self.elapsed_s = self.clock.now() - start
                    return c
                except ProviderError as e:
                    if p.outage:  # a down provider still costs a connect timeout per attempt
                        self.clock.sleep(min(1.0, self.retry.timeout_s))
                    if self.breaker_enabled:
                        br.record_failure()
                    if not e.retryable or attempt == retries:
                        break
                    if self.breaker_enabled and br.state == OPEN:
                        break  # don't keep hammering once the breaker trips
                    self.stats["retries"] += 1
                    self.clock.sleep(delays[attempt])
        self.stats["unavailable"] += 1
        self.elapsed_s = self.clock.now() - start
        raise Unavailable("no model provider available")
