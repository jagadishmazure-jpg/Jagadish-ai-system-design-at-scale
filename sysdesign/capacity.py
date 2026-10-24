"""Back-of-envelope capacity math, computed from inputs (nothing hardcoded in docs)."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

from .providers import PRICE_PER_M


@dataclass(frozen=True)
class Inputs:
    users: int = 10_000
    requests_per_user_per_day: float = 5
    peak_multiplier: float = 10
    active_hours_per_day: float = 24
    input_tokens_per_request: int = 1_500
    output_tokens_per_request: int = 300
    share_small: float = 0.55
    share_large: float = 0.15
    share_no_llm: float = 0.30
    cache_hit_rate_on_llm: float = 0.20
    mean_latency_s: float = 1.2
    days_per_month: int = 30
    uptime_target: float = 0.999


def compute(i: Inputs) -> dict:
    if abs(i.share_small + i.share_large + i.share_no_llm - 1.0) > 1e-9:
        raise ValueError("route shares must sum to 1")
    for name in ("users", "requests_per_user_per_day", "peak_multiplier", "active_hours_per_day"):
        if getattr(i, name) <= 0:
            raise ValueError(f"{name} must be positive")
    if not 0 <= i.cache_hit_rate_on_llm < 1:
        raise ValueError("cache hit rate must be in [0, 1)")
    per_day = i.users * i.requests_per_user_per_day
    avg_rps = per_day / (i.active_hours_per_day * 3600)
    peak_rps = avg_rps * i.peak_multiplier
    per_month = per_day * i.days_per_month
    llm_share = (i.share_small + i.share_large) * (1 - i.cache_hit_rate_on_llm)
    llm_calls = per_month * llm_share
    tok_per_call = i.input_tokens_per_request + i.output_tokens_per_request
    tokens_month = llm_calls * tok_per_call

    def spend(tier: str, share: float) -> float:
        pin, pout = PRICE_PER_M[tier]
        calls = per_month * share * (1 - i.cache_hit_rate_on_llm)
        return calls * (i.input_tokens_per_request * pin + i.output_tokens_per_request * pout) / 1e6

    small, large = spend("small", i.share_small), spend("large", i.share_large)
    all_large = per_month * (i.input_tokens_per_request * PRICE_PER_M["large"][0]
                             + i.output_tokens_per_request * PRICE_PER_M["large"][1]) / 1e6
    concurrency = peak_rps * i.mean_latency_s  # Little's law
    downtime_min = (1 - i.uptime_target) * i.days_per_month * 24 * 60
    return {
        "inputs": asdict(i),
        "requests_per_day": round(per_day),
        "avg_rps": round(avg_rps, 3),
        "peak_rps": round(peak_rps, 2),
        "peak_concurrent_requests": math.ceil(concurrency),
        "llm_calls_per_month": round(llm_calls),
        "tokens_per_month": round(tokens_month),
        "llm_cost_per_month_usd": round(small + large, 2),
        "all_large_model_cost_per_month_usd": round(all_large, 2),
        "allowed_downtime_min_per_month": round(downtime_min, 1),
    }
