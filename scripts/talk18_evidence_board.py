"""Talk 18 evidence board and network waterfall chart.

Reads docs/results/talk18.json (simulated, drift-checked) and docs/results/talk18_measured.json (measured
on one machine). Writes docs/img/talk18_evidence_board.png and docs/img/network_waterfall.png.
--check only re-renders into a temp file (PNG bytes vary by font/platform).
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SIM = ROOT / "docs/results/talk18.json"
MEAS = ROOT / "docs/results/talk18_measured.json"
BOARD = ROOT / "docs/img/talk18_evidence_board.png"
WATERFALL = ROOT / "docs/img/network_waterfall.png"
PHASE_COLOURS = {"dns": "#8da0cb", "tcp": "#66c2a5", "tls": "#e78ac3", "request_rtt": "#a6d854",
                 "edge_to_origin": "#ffd92f", "server": "#d62728", "transfer": "#fc8d62"}


def _plt():
    import logging

    import matplotlib
    matplotlib.use("Agg")
    matplotlib.rcParams["text.parse_math"] = False
    import matplotlib.pyplot as plt
    logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)
    return plt


def stacked_waterfall(ax, wf: dict) -> None:
    rows = wf["rows"]
    names = list(rows)
    for i, name in enumerate(names):
        left = 0.0
        for ph, v in rows[name]["phases_ms"].items():
            if v:
                ax.barh(i, v, left=left, color=PHASE_COLOURS[ph], label=ph if i == 1 or ph == "edge_to_origin" else None)
            left += v
        ax.text(left + 15, i, f"{rows[name]['total_ms']:.0f} ms (server {rows[name]['server_share']:.1%})",
                va="center", fontsize=8)
    q = wf["quoted"]
    ax.axvline(q["hotel_ms"], color="grey", linestyle="--")
    ax.text(q["hotel_ms"] + 10, len(names) - 0.4, f"talk's quoted hotel {q['hotel_ms']} ms", fontsize=8, color="grey")
    ax.set_yticks(range(len(names)), names, fontsize=8)
    ax.invert_yaxis()
    ax.set_xlim(0, max(r["total_ms"] for r in rows.values()) * 1.45)
    ax.set_xlabel("ms (simulated round-trip model)")
    handles, labels = ax.get_legend_handles_labels()
    seen = dict(zip(labels, handles))
    ax.legend(seen.values(), seen.keys(), fontsize=7, loc="lower right", ncol=2)


def draw(sim: dict, meas: dict, board: pathlib.Path, waterfall_png: pathlib.Path) -> None:
    plt = _plt()
    fig, ax = plt.subplots(figsize=(11, 4.2))
    stacked_waterfall(ax, sim["waterfall"])
    ax.set_title("One 200 KB page load: where the time goes (simulated)")
    fig.tight_layout()
    fig.savefig(waterfall_png, dpi=110)
    plt.close(fig)

    with plt.xkcd(scale=0.5, length=120, randomness=2):
        fig, ax = plt.subplots(2, 2, figsize=(15, 10))
        m = meas["machine"]
        fig.suptitle(f"Talk 18 evidence board - A, B simulated; C measured ({m['cpu_count']} CPUs, "
                     f"Python {m['python']})", fontsize=15)
        # (a) ops vs quality
        f = sim["freshness"]
        keys = ["before", "verifier_only", "after"]
        labels = ["before", "verifier only", "dated retrieval\n+ verifier"]
        import numpy as np
        x = np.arange(len(keys))
        ops = [f[k]["ops"]["error_rate"] * f[k]["n"] for k in keys]
        ax[0, 0].bar(x - 0.3, ops, width=0.2, color="#2ca02c", label="ops errors (HTTP)")
        for j, (dim, col) in enumerate((("freshness", "#d62728"), ("veracity", "#ff7f0e"), ("safety", "#9467bd"))):
            vals = [f[k]["wrong_sent"][dim] for k in keys]
            ax[0, 0].bar(x - 0.1 + 0.2 * j, vals, width=0.2, color=col, label=f"wrong answers sent: {dim}")
            for i, v in enumerate(vals):
                ax[0, 0].text(x[i] - 0.1 + 0.2 * j, v + 3, str(v), ha="center", fontsize=8)
        for i, k in enumerate(keys):
            ax[0, 0].text(x[i], 330, f"to review: {f[k]['sent_to_review']}", ha="center", fontsize=8)
        o = f["before"]["ops"]
        ax[0, 0].text(1, 200, f"ops view, all three runs: {ops[0]:.0f} HTTP errors,\np95 {o['p95_latency_s']}s, "
                      f"uptime {o['uptime']:.0%} - green", ha="center", fontsize=9, color="#2ca02c")
        ax[0, 0].set_xticks(x, labels)
        ax[0, 0].set_ylim(0, 360)
        ax[0, 0].set_title(f"(a) {f['before']['n']} answers: ops green, quality red until fixed")
        ax[0, 0].legend(fontsize=7, loc="center right")
        # (b) waterfall
        stacked_waterfall(ax[0, 1], sim["waterfall"])
        ax[0, 1].set_title("(b) page load (simulated): server is the thin red slice")
        # (c) loop stall
        ri, rp = meas["replay_inline"], meas["replay_process_pool"]
        vals = [ri["idle_loop"]["max_stall_ms"], ri["during_uploads"]["max_stall_ms"],
                rp["during_uploads"]["max_stall_ms"]]
        names = [f"{ri['idle_connections']} idle chats", f"{ri['jobs']} uploads\non the loop",
                 f"{rp['jobs']} uploads\nprocess pool"]
        ax[1, 0].bar(names, vals, color=["#66c2a5", "#d62728", "#2ca02c"])
        for i, v in enumerate(vals):
            ax[1, 0].text(i, v * 1.15 + 0.1, f"{v:,.1f} ms", ha="center", fontsize=9)
        ax[1, 0].set_yscale("log")
        from matplotlib.ticker import FuncFormatter
        ax[1, 0].yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
        ax[1, 0].set_ylabel("worst heartbeat stall (ms, log)")
        ax[1, 0].set_title(f"(c) measured loop stall; chat ping {ri['chat_ping_ms']['max']:,.0f} ms -> "
                           f"{rp['chat_ping_ms']['max']:,.1f} ms")
        # (d) experiments
        io, cpu = meas["io_experiment"], meas["cpu_experiment"]
        bars = [("5k sleeps\nthreads(256)", io["thread_pool_s"], "#8da0cb"),
                ("5k sleeps\nasyncio", io["asyncio_s"], "#2ca02c"),
                ("150 prime\nserial", cpu["serial_s"], "#bbbbbb"),
                ("150 prime\nthreads", cpu["threads_s"], "#d62728"),
                ("150 prime\nasyncio", cpu["asyncio_s"], "#ff7f0e"),
                (f"150 prime\n{cpu['processes']} procs", cpu["process_pool_s"], "#2ca02c")]
        ax[1, 1].bar([b[0] for b in bars], [b[1] for b in bars], color=[b[2] for b in bars])
        for i, b in enumerate(bars):
            ax[1, 1].text(i, b[1] + 0.03, f"{b[1]:.2f}s", ha="center", fontsize=9)
        ax[1, 1].tick_params(axis="x", labelsize=8)
        ax[1, 1].set_ylabel("seconds (measured)")
        ax[1, 1].set_title(f"(d) waiting: async wins; CPU: only processes ({cpu['process_speedup']}x)")
        fig.tight_layout()
        fig.savefig(board, dpi=100)
        plt.close(fig)


def main(argv) -> int:
    sim, meas = json.loads(SIM.read_text()), json.loads(MEAS.read_text())
    if "--check" in argv:
        a, b = ROOT / "t18_board.check.png", ROOT / "t18_wf.check.png"
        draw(sim, meas, a, b)
        a.unlink()
        b.unlink()
        print("talk 18 figures render")
        return 0
    draw(sim, meas, BOARD, WATERFALL)
    print(f"wrote {BOARD.relative_to(ROOT)} and {WATERFALL.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
