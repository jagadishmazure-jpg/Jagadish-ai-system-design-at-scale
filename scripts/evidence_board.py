"""Talk 15 evidence board: four panels from the incident replay's real (simulated) numbers.

Writes docs/img/evidence_board.png and docs/results/evidence_board.json.
--check re-renders and fails if the JSON numbers drift (PNG bytes vary by font/platform).
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from sysdesign.ext.llm_cache import (  # noqa: E402
    AUGUST,
    CACHED_DISCOUNT,
    PRICE_IN_PER_M,
    PRICE_OUT_PER_M,
    replay_incident,
)

PNG = ROOT / "docs/img/evidence_board.png"
JSON = ROOT / "docs/results/evidence_board.json"


def numbers() -> dict:
    inc = replay_incident()
    a, s = inc["august_timestamp_first"], inc["august_stable_first"]
    docs = AUGUST.doc_tokens * AUGUST.docs_per_turn
    user_fmt = AUGUST.question_tokens + 25  # question + timestamp/merchant line
    hist = round(a["avg_input_tokens"] - AUGUST.system_tokens - docs - user_fmt, 1)
    in_usd = a["avg_input_tokens"] * PRICE_IN_PER_M / 1e6
    out_usd = AUGUST.output_tokens * PRICE_OUT_PER_M / 1e6
    cached_after = round(s["token_hit_rate"] * s["avg_input_tokens"], 1)
    in_after = ((s["avg_input_tokens"] - cached_after) * PRICE_IN_PER_M
                + cached_after * PRICE_IN_PER_M * CACHED_DISCOUNT) / 1e6
    static_day = AUGUST.system_tokens * AUGUST.turns_per_day
    return {
        "note": "Simulated incident replay, illustrative prices. Timeline months other than June and August "
                "are story points, not measurements.",
        "a_tokens_per_turn": {"system_prompt": AUGUST.system_tokens, "retrieved_docs": docs,
                              "history": hist, "user_message_and_format": user_fmt},
        "b_cost_per_turn": {"input_usd": round(in_usd, 5), "output_usd": round(out_usd, 5),
                            "input_pct": round(in_usd / (in_usd + out_usd), 4),
                            "input_usd_per_output_usd": round(in_usd / out_usd, 2)},
        "c_timeline": [["Jan", 1200, ""], ["Feb", 1200, ""], ["Mar", 1500, "tool schemas"], ["Apr", 1500, ""],
                       ["May", 1500, ""], ["Jun", round(inc["june"]["avg_input_tokens"]), "policies"],
                       ["Jul", 4800, "bigger system prompt"],
                       ["Aug", round(a["avg_input_tokens"]), "more retrieval"],
                       ["Sep", round(a["avg_input_tokens"]), ""]],
        "d_static": {"static_tokens": AUGUST.system_tokens, "turns_per_day": AUGUST.turns_per_day,
                     "identical_tokens_per_day": static_day,
                     "usd_per_day_resending": round(static_day * PRICE_IN_PER_M / 1e6, 2),
                     "usd_per_day_if_cached": round(static_day * PRICE_IN_PER_M * CACHED_DISCOUNT / 1e6, 2)},
        "after": {"cached_tokens_per_turn": cached_after,
                  "uncached_tokens_per_turn": round(s["avg_input_tokens"] - cached_after, 1),
                  "input_usd_per_turn": round(in_after, 5), "ttft_p50_before": a["ttft_p50_s"],
                  "ttft_p50_after": s["ttft_p50_s"], "monthly_before": a["monthly_bill_usd"],
                  "monthly_after": s["monthly_bill_usd"]},
    }


def draw(n: dict, path: pathlib.Path) -> None:
    import logging

    import matplotlib
    matplotlib.use("Agg")
    matplotlib.rcParams["text.parse_math"] = False
    import matplotlib.pyplot as plt
    logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)

    with plt.xkcd(scale=0.6, length=120, randomness=2):
        fig, ax = plt.subplots(2, 2, figsize=(14, 9.5))
        fig.suptitle("Kestrel (Tidewell storefronts): evidence board - simulated replay", fontsize=16)
        # (a)
        a = n["a_tokens_per_turn"]
        bottom = 0
        colors = ["#8da0cb", "#66c2a5", "#fc8d62", "#e78ac3"]
        for (k, v), c in zip(a.items(), colors):
            ax[0, 0].bar(["before"], [v], bottom=bottom, color=c, label=f"{k.replace('_', ' ')} {v:,.0f}")
            bottom += v
        af = n["after"]
        ax[0, 0].bar(["after (reordered)"], [af["cached_tokens_per_turn"]], color="#bbbbbb",
                     label=f"cached {af['cached_tokens_per_turn']:,.0f}")
        ax[0, 0].bar(["after (reordered)"], [af["uncached_tokens_per_turn"]], bottom=af["cached_tokens_per_turn"],
                     color="#d62728", label=f"uncached {af['uncached_tokens_per_turn']:,.0f}")
        ax[0, 0].set_title("(a) input tokens per turn")
        ax[0, 0].legend(fontsize=8, loc="upper right")
        ax[0, 0].set_ylim(0, bottom * 1.45)
        # (b)
        b = n["b_cost_per_turn"]
        ax[0, 1].bar(["input", "output", "input after"], [b["input_usd"], b["output_usd"], af["input_usd_per_turn"]],
                     color=["#d62728", "#1f77b4", "#2ca02c"])
        ax[0, 1].set_title(f"(b) $ per turn: input is {b['input_pct']:.0%}, "
                           f"${b['input_usd_per_output_usd']} in per $1 out")
        for i, v in enumerate([b["input_usd"], b["output_usd"], af["input_usd_per_turn"]]):
            ax[0, 1].text(i, v, f"${v:.4f}", ha="center", va="bottom", fontsize=10)
        # (c)
        tl = n["c_timeline"]
        ax[1, 0].step(range(len(tl)), [t[1] for t in tl], where="post", color="#d62728")
        ax[1, 0].set_xticks(range(len(tl)), [t[0] for t in tl])
        for i, (_, v, label) in enumerate(tl):
            if label:
                ax[1, 0].annotate(f"+{label}", (i, v), xytext=(i - 1.2, v + 900), fontsize=9,
                                  arrowprops={"arrowstyle": "->"})
        ax[1, 0].set_ylim(0, max(t[1] for t in tl) * 1.35)
        ax[1, 0].set_title("(c) input tokens per turn over nine months")
        # (d)
        d = n["d_static"]
        ax[1, 1].axis("off")
        ax[1, 1].set_title("(d) the number that doesn't add up")
        ax[1, 1].text(0.02, 0.85, f"{d['static_tokens']:,} static tokens\n x {d['turns_per_day']:,} turns/day\n"
                      f" = {d['identical_tokens_per_day']:,} identical tokens/day", fontsize=14, va="top")
        ax[1, 1].text(0.02, 0.38, f"re-sent at full price: ${d['usd_per_day_resending']:,.0f}/day\n"
                      f"if cached (10%): ${d['usd_per_day_if_cached']:,.0f}/day", fontsize=13, va="top",
                      color="#d62728")
        ax[1, 1].text(0.02, 0.08, f"after reorder: TTFT p50 {af['ttft_p50_before']}s -> {af['ttft_p50_after']}s,"
                      f"\nmonthly bill USD {af['monthly_before']:,.0f} -> USD {af['monthly_after']:,.0f}", fontsize=11,
                      color="#2ca02c")
        fig.tight_layout()
        fig.savefig(path, dpi=110)
        plt.close(fig)


def main(argv) -> int:
    n = numbers()
    js = json.dumps(n, indent=2) + "\n"
    if "--check" in argv:
        tmp = ROOT / "evidence_board.check.png"
        draw(n, tmp)
        tmp.unlink()
        if not JSON.exists() or JSON.read_text() != js:
            print("drift: docs/results/evidence_board.json - run python scripts/evidence_board.py")
            return 1
        print("evidence board numbers up to date; figure renders")
        return 0
    JSON.write_text(js)
    draw(n, PNG)
    print(f"wrote {PNG.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
