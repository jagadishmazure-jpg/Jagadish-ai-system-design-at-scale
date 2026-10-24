"""Fake LLM providers: no API keys, configurable latency, failures, overload and token usage.

Each provider behaves like a hosted model endpoint:
- base latency plus per-output-token latency (so streaming has a real time-to-first-token),
- a soft capacity: above it, latency grows with the number of in-flight calls,
- a hard capacity: above it, the provider answers with a provider-side 429,
- an outage switch and a random failure rate (seeded, so runs are reproducible),
- token counting (about 1.3 tokens per word) and a price per 1k input/output tokens.
Prices are illustrative, as of 2026-10, not any vendor's list price.
"""
from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass, field


class ProviderError(Exception):
    """Transient provider failure (5xx-like)."""


class ProviderOverloaded(ProviderError):
    """Provider-side 429."""


def count_tokens(text: str) -> int:
    words = len(text.split())
    return max(1, int(round(words * 1.3)))


@dataclass
class Usage:
    tokens_in: int
    tokens_out: int
    cost_usd: float


@dataclass
class Completion:
    text: str
    usage: Usage
    model: str
    model_version: str


@dataclass
class FakeProvider:
    name: str
    model_version: str = "2026-10"
    base_latency_s: float = 0.02
    per_token_s: float = 0.0005
    price_in_per_1k: float = 0.0005
    price_out_per_1k: float = 0.0015
    soft_capacity: int = 32
    hard_capacity: int = 128
    failure_rate: float = 0.0
    hang_s: float = 0.0  # when >0, calls during an outage hang this long before failing (slow dependency)
    invalid_json_rate: float = 0.0
    seed: int = 7
    outage: bool = False
    hosting: str = "self_hosted"   # or "third_party": sensitivity routing keeps raw PII off third parties
    record_prompts: bool = True
    prompts: list = field(default_factory=list)
    inflight: int = 0
    calls: int = 0
    rng: random.Random = field(init=False)

    def __post_init__(self) -> None:
        self.rng = random.Random(self.seed)

    def cost(self, tin: int, tout: int) -> float:
        return tin / 1000 * self.price_in_per_1k + tout / 1000 * self.price_out_per_1k

    def _answer(self, prompt: str, max_tokens: int) -> str:
        words = (f"[{self.name}] Here is what I found: " + _summarise(prompt)).split()
        return " ".join(words[: max(1, int(max_tokens / 1.3))])

    async def _enter(self, prompt: str = "") -> None:
        self.calls += 1
        if self.record_prompts and len(self.prompts) < 10_000:
            self.prompts.append(prompt)
        if self.outage:
            if self.hang_s:
                await asyncio.sleep(self.hang_s)
            raise ProviderError(f"{self.name} unavailable")
        if self.inflight >= self.hard_capacity:
            raise ProviderOverloaded(f"{self.name} overloaded")
        if self.failure_rate and self.rng.random() < self.failure_rate:
            raise ProviderError(f"{self.name} transient error")
        self.inflight += 1

    def _slowdown(self) -> float:
        over = max(0, self.inflight - self.soft_capacity)
        return 1.0 + over / max(1, self.soft_capacity) * 4

    async def complete(self, prompt: str, max_tokens: int = 120) -> Completion:
        await self._enter(prompt)
        try:
            text = self._answer(prompt, max_tokens)
            tout = count_tokens(text)
            await asyncio.sleep((self.base_latency_s + tout * self.per_token_s) * self._slowdown())
            tin = count_tokens(prompt)
            return Completion(text, Usage(tin, tout, self.cost(tin, tout)), self.name, self.model_version)
        finally:
            self.inflight -= 1

    async def stream(self, prompt: str, max_tokens: int = 120):
        """Yield words; the first one arrives after the base latency (the TTFT)."""
        await self._enter(prompt)
        try:
            slow = self._slowdown()
            await asyncio.sleep(self.base_latency_s * slow)
            for w in self._answer(prompt, max_tokens).split():
                yield w
                await asyncio.sleep(self.per_token_s * 1.3 * slow)
        finally:
            self.inflight -= 1

    async def extract(self, prompt: str, schema_fields: dict) -> tuple[str, Usage]:
        """Structured-output call. Returns raw JSON text, sometimes invalid (invalid_json_rate)."""
        await self._enter(prompt)
        try:
            await asyncio.sleep(self.base_latency_s)
            payload = {k: _guess_field(prompt, k, t) for k, t in schema_fields.items()}
            raw = json.dumps(payload)
            if self.invalid_json_rate and self.rng.random() < self.invalid_json_rate:
                raw = raw[: len(raw) // 2]  # truncated JSON, like a cut-off generation
            tin, tout = count_tokens(prompt), count_tokens(raw)
            return raw, Usage(tin, tout, self.cost(tin, tout))
        finally:
            self.inflight -= 1


def _summarise(prompt: str) -> str:
    # Answer from the CONTEXT section only; the fake model "grounds" on what it is given.
    if "CONTEXT:" in prompt:
        ctx = prompt.split("CONTEXT:", 1)[1].split("QUESTION:", 1)[0]
        return " ".join(ctx.split()[:60])
    return " ".join(prompt.split()[:40])


def _guess_field(prompt: str, name: str, typ: str):
    import re
    if name == "order_id":
        m = re.search(r"\bQM-\d{5}\b", prompt)
        return m.group(0) if m else None
    if name == "amount":
        m = re.search(r"\$(\d+(?:\.\d{1,2})?)", prompt)
        return float(m.group(1)) if m else None
    if name == "reason":
        return "damaged" if "damag" in prompt.lower() else "other"
    return None


def default_providers(seed: int = 7) -> dict[str, FakeProvider]:
    """small (self-hosted, cheap, fast), large (third-party) (expensive, slower), fallback (second vendor for the large tier)."""
    return {
        "small": FakeProvider("small-model", base_latency_s=0.010, per_token_s=0.0002,
                              price_in_per_1k=0.00015, price_out_per_1k=0.0006, seed=seed),
        "large": FakeProvider("large-model", base_latency_s=0.040, per_token_s=0.0006,
                              price_in_per_1k=0.0025, price_out_per_1k=0.01, seed=seed + 1, hosting="third_party"),
        "fallback": FakeProvider("fallback-model", base_latency_s=0.050, per_token_s=0.0007,
                                 price_in_per_1k=0.003, price_out_per_1k=0.012, seed=seed + 2, hosting="third_party"),
    }
