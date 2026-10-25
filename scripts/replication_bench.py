"""Replication numbers (primary-replica, failover, bootstrap, leaderless, CDC, two-region writes).

python scripts/replication_bench.py          -> docs/results/replication.json + tables in
                                                docs/replication.md and README.md
python scripts/replication_bench.py --check  -> fails if the committed file or tables differ from a fresh run
Deterministic (fixed seeds), so CI re-runs it exactly. Simulated times, exact counts.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from sysdesign.ext import multiregion, replication  # noqa: E402

OUT = ROOT / "docs/results/replication.json"
DOCS = {"docs/replication.md": ("REPL_MODES", "REPL_RYW", "REPL_FAILOVER", "REPL_BOOTSTRAP", "REPL_CONFLICTS",
                                "REPL_SKEW", "REPL_HYBRID", "REPL_LEADERLESS", "REPL_CDC"),
        "README.md": ("REPL_README",)}


def pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def compute() -> dict:
    return {"primary_replica": replication.results(), "multi_region": multiregion.results()}


def blocks(r: dict) -> dict:
    p, m = r["primary_replica"], r["multi_region"]
    b = {}
    rows = ["| Mode | Write p50 | Write p95 | Acked writes lost, one crash at end of 4,000 writes | "
            "Lost over 200 random crashes (busy stream) | Crashes with any loss |",
            "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for k in replication.MODES:
        x, c = p["modes"][k], p["crash_trials"][k]
        rows.append(f"| {k} | {x['write_p50_ms']} ms | {x['write_p95_ms']} ms | {x['acked_writes_lost_on_crash']} | "
                    f"{c['acked_writes_lost_total']} | {c['trials_with_loss']} of {c['trials']} |")
    b["REPL_MODES"] = "\n".join(rows)
    rows = ["| Read routing after a save | Stale reads | Reads sent to the primary |", "| --- | ---: | ---: |"]
    names = {"replica": "always a replica", "primary_window": "primary for 100 ms after a write",
             "lsn_token": "write-position token (replica only if caught up)"}
    for k, x in p["read_your_writes"].items():
        rows.append(f"| {names[k]} | {x['stale_reads']} of {x['sessions']} ({pct(x['stale_read_rate'])}) | "
                    f"{pct(x['primary_read_share'])} |")
    b["REPL_RYW"] = "\n".join(rows)
    rows = ["| Run | Detect | Writes unavailable | Acked writes lost | Late writes to the old primary | "
            "Accepted (split brain) |", "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for k, x in p["failover"].items():
        rows.append(f"| {k.replace('_', ' ')} | {x['detect_ms']} ms | {x['writes_unavailable_ms']} ms | "
                    f"{x['acked_writes_lost']} | {x['late_writes_to_old_primary']} | {x['split_brain_writes_accepted']} |")
    b["REPL_FAILOVER"] = "\n".join(rows)
    bs, br = p["bootstrap"], p["bootstrap_real"]
    b["REPL_BOOTSTRAP"] = "\n".join([
        "| Way to build a new replica | Catch-up time | Read from the primary |", "| --- | ---: | ---: |",
        f"| Full copy from the primary ({bs['size_mb'] / 1000:.1f} GB) | {bs['full_copy_s']} s | "
        f"{bs['rows_read_from_primary_full']:,} rows |",
        f"| Snapshot of an existing replica + log tail | {bs['snapshot_plus_tail_s']} s | "
        f"{bs['log_entries_read_from_primary_snapshot']:,} log entries |", "",
        f"Speed-up x{bs['speedup']}; {pct(bs['primary_read_reduction'])} less read from the primary. "
        f"Real-object check: snapshot at position {br['snapshot_lsn']}, {br['tail_entries_pulled']} tail entries "
        f"pulled instead of {br['full_entries_pulled']}, contents match the primary: {br['matches_primary']}."])
    c = m["conflicts"]
    b["REPL_CONFLICTS"] = "\n".join([
        f"{c['records']} records x {c['writes'] // c['records']} writes, {c['delay_ms']:g} ms between regions, "
        f"clocks in sync. {c['records_with_concurrency']} records saw at least one pair of concurrent edits.", "",
        "| Strategy | Updates silently dropped | Flagged for a decision |", "| --- | ---: | ---: |",
        f"| Last-writer-wins (wall clock) | {c['lww_wall_dropped']} of {c['writes']} ({pct(c['lww_wall_drop_rate'])}) | 0 |",
        f"| Last-writer-wins (hybrid logical clock) | {c['lww_hlc_dropped']} ({pct(c['lww_hlc_drop_rate'])}) | 0 |",
        f"| Version vectors (keep siblings) | {c['vv_dropped']} | {c['vv_records_flagged']} records |",
        f"| Custom merge (union facts, per-field preferences) | {c['merge_dropped']} | "
        f"{c['merge_fields_flagged']} preference fields |"])
    rows = ["| West clock behind by | LWW wall: dropped | of which only because of the clock | LWW hybrid clock: dropped "
            "| of which because of the clock | Custom merge: dropped |", "| ---: | ---: | ---: | ---: | ---: | ---: |"]
    for s in m["skew_sweep"]:
        rows.append(f"| {s['skew_ms']} ms | {s['lww_wall_dropped']} | {s['lww_wall_wrong_order']} | "
                    f"{s['lww_hlc_dropped']} | {s['lww_hlc_wrong_order']} | {s['merge_dropped']} |")
    b["REPL_SKEW"] = "\n".join(rows)
    rows = ["| Commit mode | Write p50 | Write p95 | Acked writes lost, node fails (mean) | "
            "Acked writes lost, region fails (mean / p95) |", "| --- | ---: | ---: | ---: | ---: |"]
    for k, x in m["hybrid"].items():
        rows.append(f"| {k} | {x['write_p50_ms']} ms | {x['write_p95_ms']} ms | {x['node_failure_acked_lost_mean']} | "
                    f"{x['region_failure_acked_lost_mean']} / {x['region_failure_acked_lost_p95']} |")
    b["REPL_HYBRID"] = "\n".join(rows)
    rows = ["| N | W | R | W+R>N | Read repair | Stale reads | Node copies behind at end |",
            "| ---: | ---: | ---: | --- | --- | ---: | ---: |"]
    for x in p["leaderless"]:
        rows.append(f"| {x['n']} | {x['w']} | {x['r']} | {'yes' if x['overlap'] else 'no'} | "
                    f"{'on' if x['read_repair'] else 'off'} | {x['stale_reads']} of {x['reads']} "
                    f"({pct(x['stale_read_rate'])}) | {x['node_copies_behind_at_end']} |")
    b["REPL_LEADERLESS"] = "\n".join(rows)
    d = p["cdc"]
    b["REPL_CDC"] = "\n".join([
        "| Measure | Value |", "| --- | ---: |",
        f"| Changes committed (data + outbox row in one transaction) | {d['events_committed']} |",
        f"| Index staleness p50 / p95 / max (poll every {d['poll_ms']:g} ms) | {d['staleness_p50_ms']} / "
        f"{d['staleness_p95_ms']} / {d['staleness_max_ms']} ms |",
        f"| Consumer crashes before saving its position | {d['consumer_crashes']} |",
        f"| Events replayed after those crashes | {d['events_replayed']} |",
        f"| Documents where index and database disagree at the end | {d['index_docs_mismatched']} of {d['docs_in_db']} |",
        f"| App crashes inside the transaction (nothing committed, nothing to lose) | {d['aborted_transactions']} |",
        f"| Index updates a dual write would lose at those same crash points | {d['dual_write_index_updates_lost']} |"])
    mo, ct, hy, sk = p["modes"], p["crash_trials"], m["hybrid"], m["skew_sweep"][-1]
    b["REPL_README"] = (
        f"Simulated (fixed seeds), exact counts. Async commits in {mo['async']['write_p50_ms']} ms but lost "
        f"{ct['async']['acked_writes_lost_total']} acknowledged writes over {ct['async']['trials']} crashes; semi-sync "
        f"({mo['semi-sync']['write_p50_ms']} ms p50) and sync ({mo['sync']['write_p50_ms']} ms) lost 0. A write-position "
        f"token cut stale read-backs from {pct(p['read_your_writes']['replica']['stale_read_rate'])} to "
        f"{pct(p['read_your_writes']['lsn_token']['stale_read_rate'])}. Snapshot + log tail built a replica "
        f"x{p['bootstrap']['speedup']} faster than a full copy. With the west clock {sk['skew_ms']} ms behind, "
        f"last-writer-wins dropped {sk['lww_wall_dropped']} of {sk['writes']} memory and preference edits "
        f"({sk['lww_wall_wrong_order']} only because of the clock); a hybrid logical clock removed the clock-caused "
        f"drops and the custom merge dropped {sk['merge_dropped']}. Local-sync + remote-async: "
        f"{hy['local-sync+remote-async']['write_p50_ms']} ms p50 vs {hy['all-sync']['write_p50_ms']} ms all-sync, "
        f"at {hy['local-sync+remote-async']['region_failure_acked_lost_mean']} writes at risk per region loss. "
        f"Outbox CDC kept the vector index within {p['cdc']['staleness_p95_ms']} ms (p95) with 0 mismatches.")
    return b


def render_docs(r: dict, write: bool) -> list[str]:
    b = blocks(r)
    stale = []
    for f, keys in DOCS.items():
        path = ROOT / f
        old = path.read_text()
        new = old
        for k in keys:
            if f"<!-- BEGIN {k} -->" not in old:
                stale.append(f"{f}: missing block {k}")
            new = re.sub(rf"(<!-- BEGIN {k} -->).*?(<!-- END {k} -->)",
                         lambda mm, v=b[k]: mm.group(1) + "\n" + v + "\n" + mm.group(2), new, flags=re.S)
        if new != old:
            stale.append(f"{f} replication numbers are stale")
            if write:
                path.write_text(new)
    return stale


def main(argv) -> int:
    js = json.dumps(compute(), indent=2) + "\n"
    r = json.loads(js)
    if "--check" in argv:
        bad = render_docs(r, write=False)
        if not OUT.exists() or OUT.read_text() != js:
            bad.append("docs/results/replication.json differs from a fresh run")
        if bad:
            print("drift: " + "; ".join(bad) + " - run python scripts/replication_bench.py")
            return 1
        print("replication results and doc tables up to date")
        return 0
    OUT.write_text(js)
    render_docs(r, write=True)
    print(f"wrote {OUT.relative_to(ROOT)} and the doc tables")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
