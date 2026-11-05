"""Replication evidence board.

Reads docs/results/replication.json (deterministic, drift-checked). Writes docs/img/replication_board.png and
docs/results/replication_board.json (the numbers drawn). --check re-renders into a temp file and fails if the
board numbers drift from the results.
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
RES = ROOT / "docs/results/replication.json"
PNG = ROOT / "docs/img/replication_board.png"
JSON = ROOT / "docs/results/replication_board.json"


def numbers() -> dict:
    r = json.loads(RES.read_text())
    p, m = r["primary_replica"], r["multi_region"]
    return {
        "note": p["note"],
        "modes": {k: {"write_p50_ms": v["write_p50_ms"], "write_p95_ms": v["write_p95_ms"],
                      "lost_200_crashes": p["crash_trials"][k]["acked_writes_lost_total"]} for k, v in p["modes"].items()},
        "ryw": {k: {"stale_read_rate": v["stale_read_rate"], "primary_read_share": v["primary_read_share"]}
                for k, v in p["read_your_writes"].items()},
        "failover": {k: {"detect_ms": v["detect_ms"], "writes_unavailable_ms": v["writes_unavailable_ms"],
                         "acked_writes_lost": v["acked_writes_lost"],
                         "split_brain_writes_accepted": v["split_brain_writes_accepted"]} for k, v in p["failover"].items()},
        "bootstrap": {k: p["bootstrap"][k] for k in ("full_copy_s", "snapshot_plus_tail_s", "speedup",
                                                      "primary_read_reduction")},
        "conflicts": {k: m["conflicts"][k] for k in ("writes", "lww_wall_dropped", "lww_hlc_dropped", "vv_dropped",
                                                      "vv_records_flagged", "merge_dropped", "merge_fields_flagged")},
        "skew": [{k: s[k] for k in ("skew_ms", "lww_wall_dropped", "lww_wall_wrong_order", "lww_hlc_dropped",
                                    "lww_hlc_wrong_order", "merge_dropped")} for s in m["skew_sweep"]],
        "hybrid": {k: {"write_p50_ms": v["write_p50_ms"], "region_failure_acked_lost_mean":
                       v["region_failure_acked_lost_mean"], "node_failure_acked_lost_mean":
                       v["node_failure_acked_lost_mean"]} for k, v in m["hybrid"].items()},
        "cdc": {k: p["cdc"][k] for k in ("staleness_p50_ms", "staleness_p95_ms", "staleness_max_ms",
                                         "consumer_crashes", "events_replayed", "index_docs_mismatched",
                                         "dual_write_index_updates_lost")},
    }


def _plt():
    import logging

    import matplotlib
    matplotlib.use("Agg")
    matplotlib.rcParams["text.parse_math"] = False
    import matplotlib.pyplot as plt
    logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)
    return plt


def _bars(a, labels, vals, color, fmt="{:g}"):
    a.bar(range(len(vals)), vals, color=color)
    top = max(vals) if max(vals) > 0 else 1
    for i, v in enumerate(vals):
        a.text(i, v + top * 0.02, fmt.format(v), ha="center", fontsize=8)
    a.set_xticks(range(len(vals)), labels, fontsize=8)
    a.set_ylim(0, top * 1.25)


def draw(n: dict, path: pathlib.Path) -> None:
    plt = _plt()
    fig, ax = plt.subplots(2, 4, figsize=(22, 10))
    fig.suptitle("Replication - simulated with fixed seeds (times are simulated ms; counts are exact counts)",
                 fontsize=14)
    a = ax[0, 0]
    ks = list(n["modes"])
    _bars(a, ks, [n["modes"][k]["write_p50_ms"] for k in ks], "#1f77b4", "{:.2f} ms")
    a2 = a.twinx()
    a2.plot(range(len(ks)), [n["modes"][k]["lost_200_crashes"] for k in ks], "o-", color="#d62728")
    for i, k in enumerate(ks):
        a2.text(i + 0.08, n["modes"][k]["lost_200_crashes"], f"lost {n['modes'][k]['lost_200_crashes']}",
                color="#d62728", fontsize=8)
    a.set_ylabel("write p50 (bars)")
    a2.set_ylabel("acked writes lost over 200 crashes (line)", color="#d62728")
    a.set_title("A. Commit mode: latency vs data lost")

    b = ax[0, 1]
    ks = list(n["ryw"])
    _bars(b, ["always replica", "primary 100 ms", "position token"], [n["ryw"][k]["stale_read_rate"] * 100 for k in ks],
          "#d62728", "{:.1f}%")
    for i, k in enumerate(ks):
        b.text(i, -6, f"{n['ryw'][k]['primary_read_share']:.0%} on primary", ha="center", fontsize=8)
    b.set_ylabel("stale read-backs %")
    b.set_title("B. Read-your-writes for a just-saved turn")

    c = ax[0, 2]
    ks = list(n["failover"])
    c.barh(range(len(ks)), [n["failover"][k]["writes_unavailable_ms"] for k in ks], color="#7f7f7f")
    for i, k in enumerate(ks):
        f = n["failover"][k]
        c.text(f["writes_unavailable_ms"] * 0.05, i, f"detect {f['detect_ms']:g} ms | lost {f['acked_writes_lost']} | "
               f"split-brain accepted {f['split_brain_writes_accepted']}", va="center", fontsize=8, color="white")
    c.set_yticks(range(len(ks)), [k.replace("_", " ") for k in ks], fontsize=8)
    c.set_xlabel("ms without a primary taking writes")
    c.set_title("C. Failover drill (epoch fence on unless noted)")

    d = ax[0, 3]
    bs = n["bootstrap"]
    _bars(d, ["full copy from primary", "snapshot + log tail"], [bs["full_copy_s"], bs["snapshot_plus_tail_s"]],
          "#2ca02c", "{:g} s")
    d.set_title(f"D. New replica: x{bs['speedup']} faster, {bs['primary_read_reduction']:.1%} less primary read")

    e = ax[1, 0]
    c2 = n["conflicts"]
    _bars(e, ["LWW wall", "LWW hybrid clock", "version vectors", "custom merge"],
          [c2["lww_wall_dropped"], c2["lww_hlc_dropped"], c2["vv_dropped"], c2["merge_dropped"]], "#d62728")
    e.set_ylabel(f"edits silently dropped (of {c2['writes']})")
    e.text(2, c2["lww_wall_dropped"] * 0.5, f"{c2['vv_records_flagged']} records\nflagged", ha="center", fontsize=8)
    e.text(3, c2["lww_wall_dropped"] * 0.5, f"{c2['merge_fields_flagged']} fields\nflagged", ha="center", fontsize=8)
    e.set_title("E. Two regions edit agent memory + preferences")

    f_ = ax[1, 1]
    xs = list(range(len(n["skew"])))
    f_.plot(xs, [s["lww_wall_dropped"] for s in n["skew"]], "o-", color="#d62728", label="LWW wall clock: dropped")
    f_.plot(xs, [s["lww_wall_wrong_order"] for s in n["skew"]], "o--", color="#ff7f0e",
            label="  of which only because of the clock")
    f_.plot(xs, [s["lww_hlc_dropped"] for s in n["skew"]], "s-", color="#1f77b4", label="LWW hybrid clock: dropped")
    f_.plot(xs, [s["merge_dropped"] for s in n["skew"]], "^-", color="#2ca02c", label="custom merge: dropped")
    f_.set_xticks(xs, [f"{s['skew_ms']}" for s in n["skew"]])
    f_.set_xlabel("west clock behind by (ms)")
    f_.legend(fontsize=8)
    f_.set_title("F. Clock skew: LWW picks the wrong write")

    g = ax[1, 2]
    ks = list(n["hybrid"])
    _bars(g, ["all async", "local sync\nremote async", "all sync"], [n["hybrid"][k]["write_p50_ms"] for k in ks],
          "#1f77b4", "{:g} ms")
    for i, k in enumerate(ks):
        h = n["hybrid"][k]
        g.text(i, n["hybrid"]["all-sync"]["write_p50_ms"] * 1.12 if i < 2 else n["hybrid"]["all-sync"]["write_p50_ms"] * 0.5,
               f"node loss: {h['node_failure_acked_lost_mean']:g}\nregion loss: {h['region_failure_acked_lost_mean']:g}",
               ha="center", fontsize=8, color="#d62728" if i < 2 else "white")
    g.set_ylim(0, n["hybrid"]["all-sync"]["write_p50_ms"] * 1.35)
    g.set_ylabel("write p50; red = mean acked writes at risk")
    g.set_title("G. Hybrid commit across regions")

    h = ax[1, 3]
    cd = n["cdc"]
    _bars(h, ["p50", "p95", "max"], [cd["staleness_p50_ms"], cd["staleness_p95_ms"], cd["staleness_max_ms"]],
          "#9467bd", "{:g} ms")
    h.text(-0.4, cd["staleness_max_ms"] * 1.22,
           f"{cd['consumer_crashes']} consumer crashes, {cd['events_replayed']} replays\n"
           f"index vs DB mismatches: {cd['index_docs_mismatched']}\n"
           f"dual write would lose: {cd['dual_write_index_updates_lost']}", ha="left", va="top", fontsize=8)
    h.set_ylim(0, cd["staleness_max_ms"] * 1.6)
    h.set_ylabel("commit -> vector index updated")
    h.set_title("H. Outbox CDC keeps the index and cache fresh")
    fig.tight_layout()
    fig.savefig(path, dpi=90)
    plt.close(fig)


def main(argv) -> int:
    n = numbers()
    js = json.dumps(n, indent=2) + "\n"
    if "--check" in argv:
        tmp = ROOT / "replication_board.check.png"
        draw(n, tmp)
        tmp.unlink()
        if not JSON.exists() or JSON.read_text() != js:
            print("drift: docs/results/replication_board.json - run python scripts/replication_board.py")
            return 1
        print("replication board numbers up to date; figure renders")
        return 0
    JSON.write_text(js)
    draw(n, PNG)
    print(f"wrote {PNG.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
