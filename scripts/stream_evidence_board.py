"""Talk 17 evidence board.

Reads docs/results/talk17.json (deterministic, drift-checked) and docs/results/stream_replay.json
(wall-clock replay, committed as measured). Writes docs/img/stream_evidence_board.png and
docs/results/stream_evidence_board.json. --check re-renders and fails if the board numbers drift.
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from talk17_bench import results  # noqa: E402

PNG = ROOT / "docs/img/stream_evidence_board.png"
JSON = ROOT / "docs/results/stream_evidence_board.json"
REPLAY = ROOT / "docs/results/stream_replay.json"


def numbers() -> dict:
    r = results()
    rp = json.loads(REPLAY.read_text())
    s, rt, te, sl = r["streaming"], r["retries"], r["tail_extras"], r["stateless"]
    ttft = {k: v["first_token_s"] for k, v in rp["mini_proxy"].items()}
    ttft.update({k: v["first_token_s"] for k, v in rp["real_nginx"].items()})
    return {
        "a_breakdown": s["breakdown"], "b_ttft_measured_s": ttft, "b_scale": rp["scale"],
        "c_reader_curve": s["reader_curve"], "c_ratio": s["model_over_reader"],
        "d_drop": {d["mode"]: d["tokens_generated"] for d in rp["drop_at_140"]},
        "d_cancel": {("propagated" if d["propagate_cancel"] else "not_propagated"): d["tokens_generated"]
                     for d in rp["tab_close_at_140"]},
        "e_amplification_outage": {f"{a['retry_layers']}{'+budget' if a['budget_ratio'] else ''}": a["calls_per_click"]
                                   for a in rt["amplification"] if a["fail_rate"] == 1.0},
        "f_distribution": te["distribution"], "f_quoted": te["quoted"],
        "g_page": te["page"], "g_users_hit": te["users_hit_p99"],
        "h_replica_dies": {x["setup"]: {"logouts": x["logouts"], "lost_cart_items": x["lost_cart_items"],
                                        "lost_form_sections": x["lost_form_sections"]} for x in sl["replica_dies"]},
        "h_heavy_max_over_avg": {x["setup"]: x["max_over_avg"] for x in sl["heavy_user"]},
        "i_quoted_ab": s["quoted_ab"],
    }


def draw(n: dict, path: pathlib.Path) -> None:
    import logging

    import matplotlib
    matplotlib.use("Agg")
    matplotlib.rcParams["text.parse_math"] = False
    import matplotlib.pyplot as plt
    logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)

    with plt.xkcd(scale=0.6, length=120, randomness=2):
        fig, ax = plt.subplots(3, 3, figsize=(18, 14))
        fig.suptitle("Wren (Pinecrest Goods): streaming evidence board - fake model, real sockets", fontsize=17)
        # (a) where the blank screen goes
        b = n["a_breakdown"]
        left = 0.0
        for k, col in (("network_s", "#8da0cb"), ("queue_s", "#e78ac3"), ("prefill_s", "#fc8d62"),
                       ("generation_s", "#d62728")):
            ax[0, 0].barh([0], [b[k]], left=left, color=col)
            ax[0, 0].text(left + b[k] / 2, 0.35 if b[k] < 1 else 0, f"{k[:-2]}\n{b[k]}s", ha="center", fontsize=9)
            left += b[k]
        ax[0, 0].set_yticks([])
        ax[0, 0].set_xlabel("seconds")
        ax[0, 0].set_title(f"(a) {b['buffered_blank_s']}s blank, first token ready at {b['streamed_first_token_s']}s")
        # (b) measured TTFT
        t = n["b_ttft_measured_s"]
        names = list(t)
        short = [x.replace("nginx_", "nginx ").replace("_", " ")[:28] for x in names]
        cols = ["#2ca02c" if t[k] < 2 else "#d62728" for k in names]
        ax[0, 1].barh(range(len(names)), [t[k] for k in names], color=cols)
        for i, k in enumerate(names):
            ax[0, 1].text(t[k], i, f" {t[k]:.2f}s", va="center", fontsize=8)
        ax[0, 1].set_yticks(range(len(names)), short, fontsize=7)
        ax[0, 1].invert_yaxis()
        ax[0, 1].set_xlabel("measured time to first token (s)")
        ax[0, 1].set_title("(b) one buffering hop is enough to hide the stream")
        # (c) model vs reader
        c = n["c_reader_curve"]
        ax[0, 2].plot([p["t"] for p in c], [p["shown"] for p in c], color="#1f77b4", label="tokens on screen")
        ax[0, 2].plot([p["t"] for p in c], [p["read"] for p in c], color="#2ca02c", label="tokens read (5.5/s)")
        ax[0, 2].set_xlabel("seconds")
        ax[0, 2].legend(fontsize=9)
        ax[0, 2].set_title(f"(c) the model is {n['c_ratio']}x faster than reading")
        # (d) drop and cancel
        d, k = n["d_drop"], n["d_cancel"]
        labs = ["resume\n(ledger)", "regenerate", "tab closed\ncancel sent", "tab closed\nno cancel"]
        vals = [d["resume_from_ledger"], d["regenerate"], k["propagated"], k["not_propagated"]]
        ax[1, 0].bar(range(4), vals, color=["#2ca02c", "#d62728", "#2ca02c", "#d62728"])
        for i, v in enumerate(vals):
            ax[1, 0].text(i, v, str(v), ha="center", va="bottom")
        ax[1, 0].set_xticks(range(4), labs, fontsize=9)
        ax[1, 0].set_ylabel("tokens generated")
        ax[1, 0].set_title("(d) drop or close at token 140 of 380")
        # (e) retry amplification
        e = n["e_amplification_outage"]
        ax[1, 1].bar(range(len(e)), list(e.values()), color=["#fc8d62", "#2ca02c", "#d62728", "#e78ac3"])
        for i, v in enumerate(e.values()):
            ax[1, 1].text(i, v + 0.3, f"{v:.2f} x", ha="center", va="bottom", fontsize=11)
        ax[1, 1].set_xticks(range(len(e)), [x.replace("one", "one layer").replace("all", "every layer") for x in e],
                            fontsize=8)
        ax[1, 1].set_ylabel("store calls per click")
        ax[1, 1].set_title("(e) outage: retries multiply per layer")
        # (f) mean hides the tail
        f = n["f_distribution"]
        ks = ["p50_ms", "mean_ms", "p90_ms", "p99_ms"]
        ax[1, 2].bar(range(4), [f[x] for x in ks], color=["#8da0cb", "#66c2a5", "#fc8d62", "#d62728"])
        for i, x in enumerate(ks):
            ax[1, 2].text(i, f[x], f"{f[x]:.0f}", ha="center", va="bottom")
        ax[1, 2].set_xticks(range(4), [x[:-3] for x in ks])
        ax[1, 2].set_ylabel("ms")
        ax[1, 2].set_title(f"(f) mean {f['mean_ms']:.0f}ms looks fine; p99 is {f['p99_ms']:.0f}ms")
        # (g) users hit + page
        g = n["g_users_hit"]
        ax[2, 0].plot([x["requests_per_session"] for x in g], [x["formula"] for x in g], color="#d62728",
                      label="1-(0.99)^N")
        ax[2, 0].scatter([x["requests_per_session"] for x in g], [x["simulated"] for x in g], color="#1f77b4",
                         zorder=3, label="simulated")
        ax[2, 0].set_xlabel("requests per session")
        ax[2, 0].set_ylabel("users who saw a p99-slow call")
        ax[2, 0].legend(fontsize=9)
        pg = n["g_page"]
        ax[2, 0].set_title(f"(g) 1% of calls; page of {pg['fanout']}: p99 {pg['wait_for_all']['p99_ms']:.0f}ms"
                           f" vs {pg['partial']['p99_ms']:.0f}ms partial", fontsize=10)
        # (h) stateless
        h = n["h_replica_dies"]
        setups = list(h)
        for j, (m, col) in enumerate((("logouts", "#d62728"), ("lost_cart_items", "#fc8d62"),
                                      ("lost_form_sections", "#e78ac3"))):
            ax[2, 1].bar([i + (j - 1) * 0.27 for i in range(len(setups))], [h[s][m] for s in setups], width=0.27,
                         color=col, label=m.replace("_", " "))
        ax[2, 1].set_xticks(range(len(setups)), [f"{s}\nheavy user max/avg {n['h_heavy_max_over_avg'][s]}"
                                                 for s in setups], fontsize=8)
        for i, s_ in enumerate(setups):
            if not any(h[s_].values()):
                ax[2, 1].text(i, 5, "nothing lost", ha="center", fontsize=10)
        ax[2, 1].legend(fontsize=8)
        ax[2, 1].set_title("(h) a replica dies mid-session")
        # (i) quoted
        q = n["i_quoted_ab"]
        ax[2, 2].axis("off")
        ax[2, 2].text(0.0, 0.95, "(i) Quoted from the talk, not measured:", fontsize=12, va="top")
        ax[2, 2].text(0.0, 0.75, f"2x faster model: abandonment {q['model_a']['abandonment']:.0%} -> "
                                 f"{q['model_b']['abandonment']:.0%}", fontsize=11, va="top")
        ax[2, 2].text(0.0, 0.62, f"quality {q['model_b']['quality_delta']} points", fontsize=11, va="top")
        ax[2, 2].text(0.0, 0.45, f"Measured here at scale {n['b_scale']}: streamed TTFT "
                                 f"{n['b_ttft_measured_s']['streamed_end_to_end']:.2f}s", fontsize=11, va="top")
        ax[2, 2].text(0.0, 0.32, f"vs buffered {n['b_ttft_measured_s']['incident_service_and_gateway_buffered']:.2f}s",
                      fontsize=11, va="top")
        fig.tight_layout()
        fig.savefig(path, dpi=100)
        plt.close(fig)


def main(argv) -> int:
    n = numbers()
    js = json.dumps(n, indent=2) + "\n"
    if "--check" in argv:
        tmp = ROOT / "stream_evidence_board.check.png"
        draw(n, tmp)
        tmp.unlink()
        if not JSON.exists() or JSON.read_text() != js:
            print("drift: docs/results/stream_evidence_board.json - run python scripts/stream_evidence_board.py")
            return 1
        print("stream evidence board numbers up to date; figure renders")
        return 0
    JSON.write_text(js)
    draw(n, PNG)
    print(f"wrote {PNG.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
