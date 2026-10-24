"""Fake LLM providers: configurable latency, failure rate, outages and token usage. No API keys.

Prices are illustrative list-style prices (USD per 1M tokens) for model
*tiers*, not quotes from any vendor; as-of date recorded for honesty.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

PRICES_AS_OF = "2026-10 (illustrative, not a vendor quote)"
PRICE_PER_M = {  # (input, output)
    "small": (0.10, 0.40),
    "large": (2.50, 10.00),
    "fallback-small": (0.15, 0.60),
    "fallback-large": (3.00, 15.00),
}


class ProviderError(Exception):
    def __init__(self, msg: str, retryable: bool = True, status: int = 503) -> None:
        super().__init__(msg)
        self.retryable = retryable
        self.status = status


@dataclass
class Completion:
    text: str
    model: str
    provider: str
    input_tokens: int
    output_tokens: int
    latency_s: float

    @property
    def cost_usd(self) -> float:
        return cost_usd(self.model, self.input_tokens, self.output_tokens)


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    if input_tokens < 0 or output_tokens < 0:
        raise ValueError("token counts must be non-negative")
    pin, pout = PRICE_PER_M[model]
    return (input_tokens * pin + output_tokens * pout) / 1_000_000


def count_tokens(text: str) -> int:
    """Rough tokenizer stand-in: ~1 token per 4 chars, minimum 1."""
    return max(1, (len(text) + 3) // 4)


@dataclass
class FakeProvider:
    name: str
    latency_s: dict = field(default_factory=lambda: {"small": 0.25, "large": 1.2})
    failure_rate: float = 0.0
    rate_limited: bool = False
    outage: bool = False
    seed: int = 7
    calls: int = 0

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    def model_name(self, tier: str) -> str:
        return tier if self.name == "primary" else f"fallback-{tier}"

    def sample_latency(self, tier: str) -> float:
        base = self.latency_s[tier]
        return base * (0.6 + 0.8 * self._rng.random()) + (base * 4 if self._rng.random() < 0.02 else 0.0)

    def complete(self, tier: str, prompt: str, max_output_tokens: int = 200, answer: str | None = None) -> Completion:
        self.calls += 1
        if self.outage:
            raise ProviderError(f"{self.name} unavailable", retryable=True, status=503)
        if self.rate_limited:
            raise ProviderError(f"{self.name} rate limited", retryable=True, status=429)
        if self._rng.random() < self.failure_rate:
            raise ProviderError(f"{self.name} transient error", retryable=True, status=500)
        text = answer if answer is not None else f"[{tier}] answer"
        out = min(max_output_tokens, count_tokens(text))
        return Completion(text=text, model=self.model_name(tier), provider=self.name,
                          input_tokens=count_tokens(prompt), output_tokens=out,
                          latency_s=self.sample_latency(tier))
