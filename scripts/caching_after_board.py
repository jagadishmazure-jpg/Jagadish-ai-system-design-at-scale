"""Talk 15 "after" evidence board: what reordering the prompt changed.

Reads only the committed docs/results/caching.json (simulated, drift-checked by caching_bench.py --check).
Writes docs/img/caching_after_board.png and docs/results/caching_after_board.json.
--check re-renders into a temp file and fails if the JSON numbers drift (PNG bytes vary by font/platform).
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "docs/results/caching.json"
PNG = ROOT / "docs/img/caching_after_board.png"
JSON = ROOT / "docs/results/caching_after_board.json"
KEYS = ("input_cost_per_day_usd", "cost_per_day_usd", "monthly_bill_usd", "ttft_p50_s", "ttft_p95_s",
        "request_hit_rate", "token_hit_rate", "seconds_with_traffic", "seconds_with_zero_hits", "calls")


def numbers(src: dict | None = None) -> dict:
    src = src if src is not None else json.loads(SRC.read_text())
    inc = src["incident"]
    b, a = inc["august_timestamp_first"], inc["august_stable_first"]
    return {
        "note": "Simulated incident replay (August traffic, same turns), illustrative prices. "
                "Copied from docs/results/caching.json; nothing recomputed except the percent changes.",
        "before_timestamp_first": {k: b[k] for k in KEYS},
        "after_stable_first": {k: a[k] for k in KEYS},
        "change": {
            "input_cost_reduction": inc["fix"]["input_cost_reduction"],
            "total_cost_per_day_reduction": round(1 - a["cost_per_day_usd"] / b["cost_per_day_usd"], 4),
            "ttft_p50_reduction": round(1 - a["ttft_p50_s"] / b["ttft_p50_s"], 4),
            "ttft_p95_reduction": round(1 - a["ttft_p95_s"] / b["ttft_p95_s"], 4),
        },
        "cost_curve": src["cost_curve"],
    }


def draw(n: dict, path: pathlib.Path) -> None:
    import logging

    import matplotlib
    matplotlib.use("Agg")
    matplotlib.rcParams["text.parse_math"] = False
    import matplotlib.pyplot as plt
    logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)

    b, a, c = n["before_timestamp_first"], n["after_stable_first"], n["change"]
    labels = ["before\n(timestamp first)", "after\n(stable first)"]
    red, green = "#d62728", "#2ca02c"
    with plt.xkcd(scale=0.6, length=120, randomness=2):
        fig, ax = plt.subplots(2, 2, figsize=(14, 9.5))
        fig.suptitle("Kestrel (Tidewell storefronts): after the prompt reorder - simulated replay", fontsize=16)
        # (a) cost per day
        x = [0, 1]
        ax[0, 0].bar([i - 0.2 for i in x], [b["input_cost_per_day_usd"], a["input_cost_per_day_usd"]], width=0.4,
                     color=[red, green], label="input $/day")
        ax[0, 0].bar([i + 0.2 for i in x], [b["cost_per_day_usd"], a["cost_per_day_usd"]], width=0.4,
                     color="#bbbbbb", label="total $/day")
        for i, (vi, vt) in enumerate(((b["input_cost_per_day_usd"], b["cost_per_day_usd"]),
                                      (a["input_cost_per_day_usd"], a["cost_per_day_usd"]))):
            ax[0, 0].text(i - 0.2, vi + 40, f"${vi:,.0f}", ha="center", fontsize=9)
            ax[0, 0].text(i + 0.2, vt + 40, f"${vt:,.0f}", ha="center", fontsize=9)
        ax[0, 0].set_xticks(x, labels)
        ax[0, 0].set_ylim(0, b["cost_per_day_usd"] * 1.3)
        ax[0, 0].legend(fontsize=8)
        ax[0, 0].set_title(f"(a) lower cost: input -{c['input_cost_reduction']:.1%}, "
                           f"monthly ${b['monthly_bill_usd']:,.0f} -> ${a['monthly_bill_usd']:,.0f}")
        # (b) first-token time
        ax[0, 1].bar([i - 0.2 for i in x], [b["ttft_p50_s"], a["ttft_p50_s"]], width=0.4, color=[red, green],
                     label="p50")
        ax[0, 1].bar([i + 0.2 for i in x], [b["ttft_p95_s"], a["ttft_p95_s"]], width=0.4, color="#8da0cb",
                     label="p95")
        for i, (p50, p95) in enumerate(((b["ttft_p50_s"], b["ttft_p95_s"]), (a["ttft_p50_s"], a["ttft_p95_s"]))):
            ax[0, 1].text(i - 0.2, p50 + 0.01, f"{p50:.3f}s", ha="center", fontsize=9)
            ax[0, 1].text(i + 0.2, p95 + 0.01, f"{p95:.3f}s", ha="center", fontsize=9)
        ax[0, 1].set_xticks(x, labels)
        ax[0, 1].set_ylim(0, b["ttft_p95_s"] * 1.3)
        ax[0, 1].legend(fontsize=8)
        ax[0, 1].set_title(f"(b) faster replies: first token p50 -{c['ttft_p50_reduction']:.0%}, "
                           f"p95 -{c['ttft_p95_reduction']:.0%}")
        # (c) hit rate
        ax[1, 0].bar([i - 0.2 for i in x], [b["request_hit_rate"], a["request_hit_rate"]], width=0.4,
                     color=[red, green], label="requests with a cache hit")
        ax[1, 0].bar([i + 0.2 for i in x], [b["token_hit_rate"], a["token_hit_rate"]], width=0.4,
                     color="#66c2a5", label="input tokens served from cache")
        for i, (r, t) in enumerate(((b["request_hit_rate"], b["token_hit_rate"]),
                                    (a["request_hit_rate"], a["token_hit_rate"]))):
            ax[1, 0].text(i - 0.2, r + 0.02, f"{r:.2%}", ha="center", fontsize=9)
            ax[1, 0].text(i + 0.2, t + 0.02, f"{t:.2%}", ha="center", fontsize=9)
        ax[1, 0].set_xticks(x, labels)
        ax[1, 0].set_ylim(0, 1.3)
        ax[1, 0].legend(fontsize=8, loc="upper left")
        ax[1, 0].set_title(f"(c) hit rate; busy seconds with no hit: {b['seconds_with_zero_hits']:,} -> "
                           f"{a['seconds_with_zero_hits']:,} of {a['seconds_with_traffic']:,}")
        # (d) cost curve with the two runs marked
        cc = n["cost_curve"]
        ax[1, 1].plot([p["hit_rate"] for p in cc], [p["cost_usd"] for p in cc], color="#1f77b4", marker="o",
                      label="$ per turn vs token hit rate")
        ax[1, 1].axvline(b["token_hit_rate"], color=red, linestyle="--", label=f"before {b['token_hit_rate']:.1%}")
        ax[1, 1].axvline(a["token_hit_rate"], color=green, linestyle="--", label=f"after {a['token_hit_rate']:.1%}")
        ax[1, 1].set_xlabel("share of input tokens served from cache")
        ax[1, 1].legend(fontsize=8)
        ax[1, 1].set_title(f"(d) cost curve: ${cc[0]['cost_usd']:.4f} at 0% -> ${cc[-1]['cost_usd']:.4f} at 100%")
        fig.tight_layout()
        fig.savefig(path, dpi=110)
        plt.close(fig)


def main(argv) -> int:
    n = numbers()
    js = json.dumps(n, indent=2) + "\n"
    if "--check" in argv:
        tmp = ROOT / "caching_after_board.check.png"
        draw(n, tmp)
        tmp.unlink()
        if not JSON.exists() or JSON.read_text() != js:
            print("drift: docs/results/caching_after_board.json - run python scripts/caching_after_board.py")
            return 1
        print("caching after board numbers up to date; figure renders")
        return 0
    JSON.write_text(js)
    draw(n, PNG)
    print(f"wrote {PNG.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
