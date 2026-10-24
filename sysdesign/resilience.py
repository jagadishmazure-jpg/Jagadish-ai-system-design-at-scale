"""Timeouts, bounded retries with exponential backoff + full jitter, circuit breakers,
and the model gateway (outbound: primary -> fallback provider, graceful degradation).

API gateway = inbound user traffic (see gateway.py). Model gateway = outbound calls to
model providers (this file). They solve different problems and are kept separate.
"""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field

from .clock import RealClock
from .llm import Completion, FakeProvider, ProviderError


class CircuitOpen(Exception):
    pass


class Degraded(Exception):
    """Every provider failed or is open. Callers must answer honestly, never guess."""


@dataclass
class RetryPolicy:
    max_attempts: int = 3          # total attempts, including the first
    base_s: float = 0.01
    cap_s: float = 0.2
    rng: random.Random = field(default_factory=lambda: random.Random(3))

    def delay(self, attempt: int) -> float:
        """Full jitter: uniform(0, min(cap, base*2^attempt)). attempt starts at 1."""
        return self.rng.uniform(0, min(self.cap_s, self.base_s * (2 ** attempt)))


class CircuitBreaker:
    """closed -> (N consecutive failures) -> open -> (cooldown) -> half_open -> one probe.
    Probe success closes it; probe failure re-opens it. Only one probe in flight at a time."""

    def __init__(self, failure_threshold: int = 5, cooldown_s: float = 1.0, clock=None) -> None:
        self.failure_threshold = failure_threshold
        self.cooldown_s = cooldown_s
        self.clock = clock or RealClock()
        self.state = "closed"
        self.failures = 0
        self.opened_at = 0.0
        self.probe_in_flight = False
        self.transitions: list[str] = []

    def _set(self, s: str) -> None:
        if s != self.state:
            self.transitions.append(f"{self.state}->{s}")
            self.state = s

    def allow(self) -> bool:
        if self.state == "open" and self.clock.now() - self.opened_at >= self.cooldown_s:
            self._set("half_open")
        if self.state == "closed":
            return True
        if self.state == "half_open" and not self.probe_in_flight:
            self.probe_in_flight = True
            return True
        return False

    def success(self) -> None:
        self.failures = 0
        self.probe_in_flight = False
        self._set("closed")

    def failure(self) -> None:
        self.probe_in_flight = False
        if self.state == "half_open":
            self.opened_at = self.clock.now()
            self._set("open")
            return
        self.failures += 1
        if self.failures >= self.failure_threshold:
            self.opened_at = self.clock.now()
            self._set("open")


async def call_with_resilience(fn, breaker: CircuitBreaker | None, timeout_s: float,
                               retry: RetryPolicy, stats: dict | None = None):
    """Run fn() with a timeout per attempt, retrying transient errors with backoff.
    Retries never bypass an open breaker. Returns fn's result or raises the last error."""
    last: Exception | None = None
    for attempt in range(1, retry.max_attempts + 1):
        if breaker is not None and not breaker.allow():
            raise CircuitOpen("breaker open")
        try:
            out = await asyncio.wait_for(fn(), timeout=timeout_s)
        except (TimeoutError, ProviderError) as e:
            last = e
            if breaker is not None:
                breaker.failure()
            if stats is not None:
                stats["retries"] = stats.get("retries", 0) + (1 if attempt < retry.max_attempts else 0)
            if attempt < retry.max_attempts:
                await asyncio.sleep(retry.delay(attempt))
            continue
        if breaker is not None:
            breaker.success()
        return out
    raise last if last else ProviderError("no attempts")


@dataclass
class ModelGateway:
    """Routes a tier to its primary provider, falling back to a second provider for the large tier."""
    providers: dict[str, FakeProvider]
    timeout_s: float = 0.5
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    resilience: bool = True            # load tests switch this off to show the failure mode
    breaker_threshold: int = 5
    breaker_cooldown_s: float = 1.0
    clock: object = None
    breakers: dict[str, CircuitBreaker] = field(default_factory=dict)
    fallbacks_used: int = 0
    last_used_fallback: bool = False

    def fallbacks_used_last(self, _i=None) -> bool:
        return self.last_used_fallback

    def breaker(self, name: str) -> CircuitBreaker:
        if name not in self.breakers:
            self.breakers[name] = CircuitBreaker(self.breaker_threshold, self.breaker_cooldown_s, self.clock)
        return self.breakers[name]

    def chain(self, tier: str) -> list[str]:
        if tier == "large" and "fallback" in self.providers:
            return ["large", "fallback"]
        if tier == "small" and "large" in self.providers:
            return ["small", "large"]
        return [tier]

    async def complete(self, tier: str, prompt: str, max_tokens: int = 120,
                       stats: dict | None = None, allow_third_party: bool = True) -> Completion:
        stats = stats if stats is not None else {}
        chain = [n for n in self.chain(tier)
                 if allow_third_party or self.providers[n].hosting != "third_party"]
        if not chain:
            raise Degraded("no provider allowed for this sensitivity level")
        if not self.resilience:
            # Naive mode: one provider, no timeout, no retry, no fallback.
            return await self.providers[chain[0]].complete(prompt, max_tokens)
        errors = []
        for i, name in enumerate(chain):
            p = self.providers[name]
            try:
                out = await call_with_resilience(lambda p=p: p.complete(prompt, max_tokens),
                                                 self.breaker(name), self.timeout_s, self.retry, stats)
                self.last_used_fallback = i > 0
                if i > 0:
                    self.fallbacks_used += 1
                    stats["fallback"] = name
                return out
            except (TimeoutError, ProviderError, CircuitOpen) as e:
                errors.append(f"{name}: {type(e).__name__}")
        raise Degraded("; ".join(errors))
