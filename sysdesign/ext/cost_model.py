"""Back-of-envelope capacity and cost estimator. Every number is computed from inputs.

Prices are EXAMPLE INPUTS (USD per 1M tokens, as of 2026-10), not quotes from any vendor.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

SECONDS_PER_DAY = 86_400

EXAMPLE_TIERS = {   # USD per 1M tokens (input, output) - example inputs only
    "small": (0.15, 0.60),
    "mid": (1.00, 4.00),
    "frontier": (5.00, 20.00),
}


def _nonneg(**kw) -> None:
    for k, v in kw.items():
        if v is None or (isinstance(v, float) and math.isnan(v)) or v < 0:
            raise ValueError(f"{k} must be >= 0")


@dataclass
class Estimate:
    requests_per_day: float
    avg_qps: float
    peak_qps: float
    tokens_per_day: float
    tokens_per_month: float
    cost_per_request: float
    cost_per_day: float
    cost_per_month: float
    cost_per_year: float
    cost_per_conversation: float

    def as_dict(self) -> dict:
        return {k: round(v, 6) for k, v in asdict(self).items()}


def estimate(users: float, requests_per_user_day: float, tokens_in: float, tokens_out: float,
             price_in_per_m: float, price_out_per_m: float, peak_factor: float = 3.0,
             turns_per_conversation: float = 1.0) -> Estimate:
    _nonneg(users=users, requests_per_user_day=requests_per_user_day, tokens_in=tokens_in, tokens_out=tokens_out,
            price_in_per_m=price_in_per_m, price_out_per_m=price_out_per_m)
    if not 1.0 <= peak_factor <= 10.0:
        raise ValueError("peak_factor should be between 1 and 10 (typically 2-5)")
    if turns_per_conversation < 1:
        raise ValueError("turns_per_conversation must be >= 1")
    rpd = users * requests_per_user_day
    qps = rpd / SECONDS_PER_DAY
    tok = tokens_in + tokens_out
    cpr = tokens_in / 1e6 * price_in_per_m + tokens_out / 1e6 * price_out_per_m
    return Estimate(rpd, qps, qps * peak_factor, rpd * tok, rpd * tok * 30, cpr, cpr * rpd, cpr * rpd * 30,
                    cpr * rpd * 365, cpr * turns_per_conversation)


def blended_price(mix: dict[str, float], tiers=EXAMPLE_TIERS) -> tuple[float, float]:
    if any(v < 0 for v in mix.values()):
        raise ValueError("mix shares must be >= 0")
    total = sum(mix.values())
    if abs(total - 1.0) > 1e-6:
        raise ValueError(f"mix must sum to 1, got {total}")
    return (sum(mix[t] * tiers[t][0] for t in mix), sum(mix[t] * tiers[t][1] for t in mix))


def check_budget(mix: dict[str, float], tokens_in: float, tokens_out: float, turns: float,
                 target_per_conversation: float) -> dict:
    """Common mistake guard: compare cost PER CONVERSATION (turns x per-turn cost) with the
    per-conversation target, never a per-turn figure or a daily total."""
    _nonneg(tokens_in=tokens_in, tokens_out=tokens_out, turns=turns, target=target_per_conversation)
    pin, pout = blended_price(mix)
    per_turn = tokens_in / 1e6 * pin + tokens_out / 1e6 * pout
    per_conv = per_turn * turns
    return {"per_turn": round(per_turn, 6), "per_conversation": round(per_conv, 6),
            "target": target_per_conversation, "within_budget": per_conv <= target_per_conversation,
            "per_turn_looks_ok_but_conversation_fails": per_turn <= target_per_conversation < per_conv}


def solve_mix(tokens_in: float, tokens_out: float, turns: float, target: float, frontier_share: float = 0.05,
              step: float = 0.05) -> dict | None:
    """Largest 'mid' share (better quality) that still fits the per-conversation target,
    given a fixed frontier share for complex cases. Returns None if even all-small misses."""
    best = None
    mid = 0.0
    while mid + frontier_share <= 1.0 + 1e-9:
        mix = {"small": round(1 - mid - frontier_share, 4), "mid": round(mid, 4), "frontier": frontier_share}
        r = check_budget(mix, tokens_in, tokens_out, turns, target)
        if r["within_budget"]:
            best = {"mix": mix, **r}
        mid += step
    return best


def step_cap(conversation_budget_usd: float, worst_tokens_in: float, worst_tokens_out: float,
             tier: str = "frontier") -> int:
    """Max agent steps so that steps x worst-case step cost stays within the conversation budget."""
    _nonneg(budget=conversation_budget_usd, tin=worst_tokens_in, tout=worst_tokens_out)
    pin, pout = EXAMPLE_TIERS[tier]
    worst = worst_tokens_in / 1e6 * pin + worst_tokens_out / 1e6 * pout
    if worst == 0:
        raise ValueError("worst-case step cost must be > 0")
    return int(conversation_budget_usd // worst)


def report(users=10_000_000, rpd=5, tin=2_000, tout=1_000, peak=3.0, mix=None, turns=4, target=0.01) -> dict:
    mix = mix or {"small": 0.75, "mid": 0.20, "frontier": 0.05}
    pin, pout = blended_price(mix)
    e = estimate(users, rpd, tin, tout, pin, pout, peak, turns)
    return {"inputs": {"users": users, "requests_per_user_day": rpd, "tokens_in": tin, "tokens_out": tout,
                       "peak_factor": peak, "mix": mix, "turns_per_conversation": turns,
                       "target_per_conversation_usd": target},
            "estimate": e.as_dict(), "budget": check_budget(mix, tin, tout, turns, target),
            "step_cap_frontier": step_cap(target, tin, tout)}
