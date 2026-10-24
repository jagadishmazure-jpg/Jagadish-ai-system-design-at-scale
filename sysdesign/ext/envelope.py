"""Interview back-of-envelope calculator: traffic, tokens, cost per conversation, tier mix.

Every number in docs/interview-playbook.md, docs/interview-cases/ and
docs/interview-question-bank.md is produced by this module through
scripts/render_interview.py; CI fails if the docs drift from it.

Prices are ILLUSTRATIVE inputs (USD per 1M tokens, as of 2026-10) for generic
small / mid / frontier tiers. They are not quotes from any vendor.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field

from sysdesign.ext.cost_model import EXAMPLE_TIERS

SECONDS_PER_DAY = 86_400
TIERS = ("small", "mid", "frontier")
QUALITY = {"small": 1.0, "mid": 2.0, "frontier": 3.0}  # ordinal, used only to rank mixes


def _check(name: str, v: float) -> None:
    if v is None or isinstance(v, bool) or not isinstance(v, (int, float)) or math.isnan(v) or math.isinf(v):
        raise ValueError(f"{name} must be a finite number")
    if v < 0:
        raise ValueError(f"{name} must be >= 0")


@dataclass(frozen=True)
class Scenario:
    users: float                 # distinct users (or conversations/day when sessions_per_user=1)
    sessions_per_user: float     # conversations per user per day
    turns: float                 # model requests per conversation
    system_tokens: float
    context_tokens: float
    output_tokens: float
    peak_factor: float = 3.0
    mix: dict = field(default_factory=lambda: {"small": 0.75, "mid": 0.20, "frontier": 0.05})
    prices: dict = field(default_factory=lambda: dict(EXAMPLE_TIERS))
    target_per_conversation: float | None = None

    def __post_init__(self) -> None:
        for k in ("users", "sessions_per_user", "turns", "system_tokens", "context_tokens", "output_tokens",
                  "peak_factor"):
            _check(k, getattr(self, k))
        if self.target_per_conversation is not None:
            _check("target_per_conversation", self.target_per_conversation)
        if self.peak_factor < 1:
            raise ValueError("peak_factor must be >= 1")
        for t, (pin, pout) in self.prices.items():
            _check(f"price_in[{t}]", pin)
            _check(f"price_out[{t}]", pout)
        validate_mix(self.mix, self.prices)


def validate_mix(mix: dict, prices: dict) -> None:
    for t, s in mix.items():
        if t not in prices:
            raise ValueError(f"unknown tier {t!r}")
        _check(f"mix[{t}]", s)
    if abs(sum(mix.values()) - 1.0) > 1e-6:
        raise ValueError(f"mix must sum to 1, got {sum(mix.values())}")


def cost_per_request(s: Scenario, mix: dict | None = None) -> float:
    mix = s.mix if mix is None else mix
    validate_mix(mix, s.prices)
    tin = s.system_tokens + s.context_tokens
    pin = sum(share * s.prices[t][0] for t, share in mix.items())
    pout = sum(share * s.prices[t][1] for t, share in mix.items())
    return tin / 1e6 * pin + s.output_tokens / 1e6 * pout


def compute(s: Scenario) -> dict:
    conv = s.users * s.sessions_per_user
    rpd = conv * s.turns
    qps = rpd / SECONDS_PER_DAY
    tin = (s.system_tokens + s.context_tokens) * rpd
    tout = s.output_tokens * rpd
    cpr = cost_per_request(s)
    per_day = cpr * rpd
    per_conv = cpr * s.turns
    out = {
        "conversations_per_day": conv, "requests_per_day": rpd, "avg_qps": qps, "peak_qps": qps * s.peak_factor,
        "tokens_per_request": s.system_tokens + s.context_tokens + s.output_tokens,
        "input_tokens_per_day": tin, "output_tokens_per_day": tout, "tokens_per_day": tin + tout,
        "cost_per_request": cpr, "cost_per_day": per_day, "cost_per_month": per_day * 30,
        "cost_per_conversation": per_conv,
        "all_frontier_cost_per_day": cost_per_request(s, {"frontier": 1.0}) * rpd,
    }
    if s.target_per_conversation is not None:
        out["target_per_conversation"] = s.target_per_conversation
        out["within_target"] = per_conv <= s.target_per_conversation + 1e-12
    return out


def mixes(step: float = 0.05):
    n = round(1 / step)
    for i in range(n + 1):
        for j in range(n + 1 - i):
            yield {"small": round((n - i - j) * step, 4), "mid": round(j * step, 4), "frontier": round(i * step, 4)}


def best_mix(s: Scenario, target: float, min_frontier: float = 0.0, min_mid: float = 0.0, step: float = 0.05):
    """Highest-quality mix (ties: cheapest) whose $/conversation meets the target, honouring minimum shares
    for hard cases. Also returns the cheapest feasible mix. None if nothing fits."""
    _check("target", target)
    _check("min_frontier", min_frontier)
    _check("min_mid", min_mid)
    ok = []
    for m in mixes(step):
        if m["frontier"] + 1e-9 < min_frontier or m["mid"] + 1e-9 < min_mid:
            continue
        c = cost_per_request(s, m) * s.turns
        if c <= target + 1e-12:
            q = sum(QUALITY[t] * v for t, v in m.items())
            ok.append((q, c, m))
    if not ok:
        return None
    best = max(ok, key=lambda x: (round(x[0], 6), -x[1]))
    cheap = min(ok, key=lambda x: x[1])
    return {"best_mix": best[2], "best_cost_per_conversation": best[1],
            "cheapest_mix": cheap[2], "cheapest_cost_per_conversation": cheap[1]}


def planted_mistake(s: Scenario, daily_ceiling: float) -> dict:
    """The 'under the daily figure, so we are fine' mistake: the target is per conversation."""
    r = compute(s)
    if s.target_per_conversation is None:
        raise ValueError("scenario needs target_per_conversation")
    return {"cost_per_day": r["cost_per_day"], "daily_ceiling": daily_ceiling,
            "daily_check_passes": r["cost_per_day"] <= daily_ceiling,
            "conversations_per_day": r["conversations_per_day"],
            "cost_per_conversation": r["cost_per_day"] / r["conversations_per_day"],
            "target_per_conversation": s.target_per_conversation,
            "conversation_check_passes": r["within_target"],
            "daily_ceiling_implied_per_conversation": daily_ceiling / r["conversations_per_day"]}


def agent_steps(budget_per_conversation: float, step_in: float, step_out: float, tier: str = "mid",
                prices: dict | None = None) -> int:
    for k, v in (("budget", budget_per_conversation), ("step_in", step_in), ("step_out", step_out)):
        _check(k, v)
    prices = prices or EXAMPLE_TIERS
    c = step_in / 1e6 * prices[tier][0] + step_out / 1e6 * prices[tier][1]
    if c <= 0:
        raise ValueError("step cost must be > 0")
    return int(budget_per_conversation // c)


def scale(s: Scenario, factor: float, cache_hit: float = 0.0) -> dict:
    """10x follow-up: linear growth, optionally with a response-cache hit rate removing model calls."""
    _check("factor", factor)
    _check("cache_hit", cache_hit)
    if cache_hit >= 1:
        raise ValueError("cache_hit must be < 1")
    base = compute(s)
    big = compute(Scenario(**{**s.__dict__, "users": s.users * factor}))
    return {"factor": factor, "cache_hit": cache_hit, "peak_qps": big["peak_qps"],
            "model_peak_qps": big["peak_qps"] * (1 - cache_hit),
            "cost_per_day": big["cost_per_day"] * (1 - cache_hit), "base_cost_per_day": base["cost_per_day"]}


# Worked scenarios used by the docs (fictional; illustrative prices)
TALK_10M = Scenario(10_000_000, 5, 1, 500, 2_000, 500)
TALK_200K = Scenario(200_000, 1, 8, 500, 2_000, 500, target_per_conversation=0.02)
PLANTED_CEILING = 6_000.0

CASES = {
    "bank": dict(scenario=Scenario(2_000_000, 0.5, 6, 600, 1_800, 300, target_per_conversation=0.02),
                 min_frontier=0.05, agent=(0.04, 5_000, 400, "mid")),
    "retail": dict(scenario=Scenario(1_500_000, 0.4, 5, 500, 1_500, 250, target_per_conversation=0.01),
                   min_frontier=0.0),
    "insurance": dict(scenario=Scenario(40_000, 1, 12, 400, 6_000, 600, peak_factor=4.0,
                                        target_per_conversation=0.25), min_frontier=0.10),
    "healthcare": dict(scenario=Scenario(300_000, 0.3, 7, 800, 1_200, 250, target_per_conversation=0.03),
                       min_frontier=0.15),
    "mortgage": dict(scenario=Scenario(60_000, 1, 10, 500, 8_000, 400, target_per_conversation=0.20),
                     min_frontier=0.05),
    "enterprise": dict(scenario=Scenario(50_000, 3, 2, 400, 3_000, 350, peak_factor=5.0,
                                         target_per_conversation=0.01), min_frontier=0.0),
    "lowlatency": dict(scenario=Scenario(5_000_000, 20, 1, 150, 200, 20, peak_factor=5.0,
                                         target_per_conversation=0.0002), min_frontier=0.0),
}


def case_report(name: str) -> dict:
    c = CASES[name]
    s = c["scenario"]
    r = compute(s)
    r["best"] = best_mix(s, s.target_per_conversation, min_frontier=c.get("min_frontier", 0.0))
    r["x10"] = scale(s, 10, cache_hit=0.3)
    if "agent" in c:
        r["agent_max_steps"] = agent_steps(*c["agent"])
    return r


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Back-of-envelope LLM capacity and cost (illustrative prices)")
    p.add_argument("--users", type=float, required=True)
    p.add_argument("--sessions", type=float, default=1)
    p.add_argument("--turns", type=float, default=1)
    p.add_argument("--system", type=float, default=500)
    p.add_argument("--context", type=float, default=2000)
    p.add_argument("--output", type=float, default=500)
    p.add_argument("--peak", type=float, default=3.0)
    p.add_argument("--mix", default="0.75,0.20,0.05", help="small,mid,frontier shares")
    p.add_argument("--target", type=float, default=None, help="USD per conversation")
    p.add_argument("--min-frontier", type=float, default=0.0)
    a = p.parse_args(argv)
    try:
        sm, md, fr = (float(x) for x in a.mix.split(","))
        s = Scenario(a.users, a.sessions, a.turns, a.system, a.context, a.output, a.peak,
                     {"small": sm, "mid": md, "frontier": fr}, target_per_conversation=a.target)
        out = compute(s)
        if a.target is not None:
            out["best"] = best_mix(s, a.target, a.min_frontier)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(json.dumps(out, indent=2, default=float))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
