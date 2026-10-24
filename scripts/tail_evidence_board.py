"""Talk 16 evidence board, drawn from docs/results/tail_latency.json-equivalent numbers.

Writes docs/img/tail_evidence_board.png and docs/results/tail_evidence_board.json.
--check re-renders and fails if the JSON numbers drift (PNG bytes vary by font/platform).
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from sysdesign.ext.tail_latency import DEADLINE_S, results  # noqa: E402

PNG = ROOT / "docs/img/tail_evidence_board.png"
JSON = ROOT / "docs/results/tail_evidence_board.json"


def numbers() -> dict:
    r = results()
    inc = r["incident"]
    bo = r["brownout"]
    return {
        "note": r["note"],
        "a_fanout": r["fanout_curve_p1pct"],
        "b_turns": {k: {"p50_s": inc[k]["p50_s"], "p99_s": inc[k]["p99_s"],
                        "deadline_miss_rate": inc[k]["deadline_miss_rate"]}
                    for k in ("healthy", "incident", "incident_parallel_only", "incident_fixed")},
        "b_hedge_extra_load": inc["incident_fixed"]["hedge_extra_load"],
        "c_busy": {k: bo[k]["busy_timeline"] for k in ("one_replica", "two_replicas", "fixed")},
        "c_saturation_after_s": {k: bo[k]["seconds_after_brownout"] for k in ("one_replica", "two_replicas", "fixed")},
        "c_brownout_starts_s": bo["one_replica"]["brownout_starts_s"],
        "c_littles_law": {"in_flight_30s_timeout": bo["one_replica"]["littles_law_in_flight_30s"],
                          "in_flight_with_3_retries": bo["one_replica"]["littles_law_in_flight_with_retries"]},
        "d_slices": r["budget_slices_s"],
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
        fig.suptitle("Kestrel (Tidewell storefronts): tail latency evidence board - simulated replay", fontsize=16)
        # (a) fan-out
        a = n["a_fanout"]
        ax[0, 0].plot([x["n"] for x in a], [x["formula"] for x in a], color="#d62728", label="1-(1-p)^n, p=1%")
        ax[0, 0].scatter([x["n"] for x in a], [x["simulated"] for x in a], color="#1f77b4", zorder=3,
                         label="simulated")
        ax[0, 0].set_xticks([1, 10, 20, 50, 100])
        ax[0, 0].set_xlabel("calls per turn")
        ax[0, 0].set_ylabel("turns that hit a slow call")
        ax[0, 0].set_title("(a) a 1% problem grows with every call")
        ax[0, 0].legend(fontsize=9, loc="upper left")
        # (b) p99 + misses
        b = n["b_turns"]
        labels = ["healthy", "incident", "parallel\nonly", "fixed"]
        keys = list(b)
        p99 = [b[k]["p99_s"] for k in keys]
        p50 = [b[k]["p50_s"] for k in keys]
        x = range(len(keys))
        ax[0, 1].bar([i - 0.2 for i in x], p50, width=0.4, color="#8da0cb", label="p50")
        ax[0, 1].bar([i + 0.2 for i in x], p99, width=0.4, color=["#66c2a5", "#d62728", "#fc8d62", "#2ca02c"],
                     label="p99")
        for i, k in enumerate(keys):
            ax[0, 1].text(i + 0.2, p99[i], f"{p99[i]:.1f}s\nmiss {b[k]['deadline_miss_rate']:.2%}", ha="center",
                          va="bottom", fontsize=9)
        ax[0, 1].set_xticks(list(x), labels)
        ax[0, 1].set_ylim(0, max(p99) * 1.5)
        ax[0, 1].set_title(f"(b) turn latency; hedging adds {n['b_hedge_extra_load']:.1%} calls")
        ax[0, 1].legend(fontsize=9, loc="upper right")
        # (c) busy workers
        c = n["c_busy"]
        sat = n["c_saturation_after_s"]
        for k, col, lab in (("two_replicas", "#ff7f0e", "2 replicas (200 workers)"),
                            ("one_replica", "#d62728", "1 replica (100 workers)"),
                            ("fixed", "#2ca02c", "fixed: breaker + bulkhead + budget")):
            extra = f", full {sat[k]}s after" if sat[k] is not None else ", never full"
            ax[1, 0].plot(range(len(c[k])), c[k], color=col, label=lab + extra)
        ax[1, 0].axvline(n["c_brownout_starts_s"], color="grey", linestyle="--")
        ax[1, 0].text(n["c_brownout_starts_s"] + 1, 185, "moderation hangs", fontsize=9)
        ax[1, 0].set_xlabel("seconds")
        ax[1, 0].set_ylabel("busy workers")
        ll = n["c_littles_law"]
        ax[1, 0].set_title(f"(c) Little's law: {ll['in_flight_30s_timeout']:.0f} in flight at 30s, "
                           f"{ll['in_flight_with_3_retries']:.0f} with retries")
        ax[1, 0].legend(fontsize=8, loc="center right")
        ax[1, 0].set_ylim(0, 260)
        # (d) budget
        d = n["d_slices"]
        left = 0.0
        cols = ["#8da0cb", "#66c2a5", "#e78ac3", "#fc8d62", "#bbbbbb"]
        for (k, v), col in zip(d.items(), cols):
            ax[1, 1].barh([0], [v], left=left, color=col)
            ax[1, 1].text(left + v / 2, 0.25 if v < 1 else 0, f"{k}\n{v}s", ha="center", va="center", fontsize=9)
            left += v
        ax[1, 1].set_xlim(0, DEADLINE_S)
        ax[1, 1].set_yticks([])
        ax[1, 1].set_xlabel("seconds of the 20s deadline")
        ax[1, 1].set_title("(d) each step owns a slice; only fallbacks spend leftovers")
        fig.tight_layout()
        fig.savefig(path, dpi=110)
        plt.close(fig)


def main(argv) -> int:
    n = numbers()
    js = json.dumps(n, indent=2) + "\n"
    if "--check" in argv:
        tmp = ROOT / "tail_evidence_board.check.png"
        draw(n, tmp)
        tmp.unlink()
        if not JSON.exists() or JSON.read_text() != js:
            print("drift: docs/results/tail_evidence_board.json - run python scripts/tail_evidence_board.py")
            return 1
        print("tail evidence board numbers up to date; figure renders")
        return 0
    JSON.write_text(js)
    draw(n, PNG)
    print(f"wrote {PNG.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
