"""Fill the generated number blocks in the interview docs from sysdesign.ext.envelope.

Blocks look like <!-- envelope:KEY --> ... <!-- /envelope:KEY -->. Text outside blocks is hand-written.
  python scripts/render_interview.py          # rewrite blocks
  python scripts/render_interview.py --check  # CI: fail if any block drifts or a doc has no block
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sysdesign.ext import envelope as E  # noqa: E402

DOCS = [ROOT / "docs/interview-playbook.md", ROOT / "docs/interview-question-bank.md",
        *sorted((ROOT / "docs/interview-cases").glob("*.md"))]
BLOCK = re.compile(r"(<!-- envelope:([\w:]+) -->\n)(.*?)(<!-- /envelope:\2 -->)", re.S)


def usd(x: float) -> str:
    if x >= 100:
        return f"${x:,.0f}"
    if x >= 1:
        return f"${x:,.2f}"
    if x >= 0.01:
        return f"${x:.4f}".rstrip("0")
    return f"${x:.6f}".rstrip("0")


def n(x: float) -> str:
    return f"{x:,.0f}" if abs(x - round(x)) < 1e-9 or x >= 100 else f"{x:,.1f}"


def big(x: float) -> str:
    for d, s in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if x >= d:
            return f"{x / d:,.2f}".rstrip("0").rstrip(".") + s
    return n(x)


def mix(m: dict) -> str:
    return " / ".join(f"{round(m[t] * 100)}% {t}" for t in E.TIERS)


def table(rows) -> str:
    return "| Quantity | Value |\n|---|---|\n" + "".join(f"| {a} | {b} |\n" for a, b in rows)


def traffic_rows(s: E.Scenario, r: dict):
    return [("Conversations per day", big(r["conversations_per_day"])),
            ("Model requests per day", big(r["requests_per_day"])),
            ("Average QPS (requests / 86,400)", n(r["avg_qps"])),
            (f"Peak QPS (x{s.peak_factor:g})", n(r["peak_qps"])),
            ("Tokens per request (system + context + output)",
             f"{n(s.system_tokens)} + {n(s.context_tokens)} + {n(s.output_tokens)} = {n(r['tokens_per_request'])}"),
            ("Input tokens per day", big(r["input_tokens_per_day"])),
            ("Output tokens per day", big(r["output_tokens_per_day"])),
            ("Tokens per day", big(r["tokens_per_day"])),
            (f"Cost per day at {mix(s.mix)}", usd(r["cost_per_day"])),
            ("Cost per day if everything went to frontier", usd(r["all_frontier_cost_per_day"])),
            ("Cost per conversation", usd(r["cost_per_conversation"]))]


def render(key: str) -> str:
    if key == "prices":
        return ("| Tier | Input $/1M | Output $/1M |\n|---|---|---|\n" +
                "".join(f"| {t} | {p[0]:.2f} | {p[1]:.2f} |\n" for t, p in E.EXAMPLE_TIERS.items()))
    if key == "talk10m":
        return table(traffic_rows(E.TALK_10M, E.compute(E.TALK_10M)))
    if key == "talk200k":
        s = E.TALK_200K
        r = E.compute(s)
        b = E.best_mix(s, s.target_per_conversation, min_frontier=0.05)
        return table(traffic_rows(s, r) + [
            ("Target per conversation", usd(s.target_per_conversation)),
            ("Meets target at the talk's mix?", "yes" if r["within_target"] else "no"),
            ("Best-quality mix that fits (frontier >= 5%)",
             f"{mix(b['best_mix'])} = {usd(b['best_cost_per_conversation'])}/conversation")])
    if key == "planted":
        p = E.planted_mistake(E.TALK_200K, E.PLANTED_CEILING)
        return table([("Daily cost", usd(p["cost_per_day"])), ("Daily figure it was compared with", usd(p["daily_ceiling"])),
                      ("Daily comparison says", "under" if p["daily_check_passes"] else "over"),
                      ("Conversations per day", big(p["conversations_per_day"])),
                      ("Cost per conversation (daily cost / conversations)", usd(p["cost_per_conversation"])),
                      ("Per-conversation target", usd(p["target_per_conversation"])),
                      ("Per-conversation comparison says", "under" if p["conversation_check_passes"] else "over"),
                      ("What the daily figure silently allowed per conversation",
                       usd(p["daily_ceiling_implied_per_conversation"]))])
    if key.startswith("case:"):
        name = key.split(":", 1)[1]
        c = E.CASES[name]
        s = c["scenario"]
        r = E.case_report(name)
        rows = traffic_rows(s, r) + [("Target per conversation", usd(s.target_per_conversation)),
                                     ("Meets target at the starting mix?", "yes" if r["within_target"] else "no")]
        b = r["best"]
        if b:
            rows += [(f"Best-quality mix that fits (frontier >= {round(c.get('min_frontier', 0) * 100)}%)",
                      f"{mix(b['best_mix'])} = {usd(b['best_cost_per_conversation'])}"),
                     ("Cheapest feasible mix", f"{mix(b['cheapest_mix'])} = {usd(b['cheapest_cost_per_conversation'])}")]
        else:
            rows.append(("Feasible mix", "none: change tokens, caching or the target"))
        x = r["x10"]
        rows += [("10x users: peak QPS", n(x["peak_qps"])),
                 ("10x users with 30% cache hits: model peak QPS", n(x["model_peak_qps"])),
                 ("10x users with 30% cache hits: cost per day", usd(x["cost_per_day"]))]
        if "agent_max_steps" in r:
            a = c["agent"]
            rows.append((f"Agent step cap ({usd(a[0])} budget, {n(a[1])} in + {n(a[2])} out per step, {a[3]})",
                         str(r["agent_max_steps"])))
        return table(rows)
    raise KeyError(key)


def main() -> int:
    check = "--check" in sys.argv
    bad = []
    for p in DOCS:
        if not p.exists():
            bad.append(f"missing {p.relative_to(ROOT)}")
            continue
        text = p.read_text()
        new, count = BLOCK.subn(lambda m: m.group(1) + render(m.group(2)) + m.group(4), text)
        if count == 0:
            bad.append(f"{p.relative_to(ROOT)} has no envelope block")
        if new != text:
            if check:
                bad.append(f"{p.relative_to(ROOT)} drifted from envelope.py")
            else:
                p.write_text(new)
    if bad:
        print("\n".join(bad))
        return 1
    print(f"interview docs match envelope.py ({len(DOCS)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
