"""Two-region multi-primary: conflicts, clocks, merge, hybrid commit. Simulated with fixed seeds."""
import json
import pathlib
import random
import subprocess
import sys

import pytest

from sysdesign.ext import multiregion as M

ROOT = pathlib.Path(__file__).resolve().parent.parent


def W(wid, region, t, kind, fld, value, vv, wall=None, hlc=None):
    return M.Write(wid, region, t, kind, fld, value, vv, wall or (t, region), hlc or (t, 0, region))


def test_version_vector_order_and_concurrency():
    assert M.vv_leq({"east": 1}, {"east": 2, "west": 1})
    assert M.vv_concurrent({"east": 1}, {"west": 1})
    assert not M.vv_concurrent({"east": 1}, {"east": 1, "west": 1})
    assert M.vv_merge({"east": 2}, {"east": 1, "west": 3}) == {"east": 2, "west": 3}


def test_hybrid_clock_never_goes_backwards():
    h = M.HybridClock()
    a = h.now(100.0)
    b = h.now(90.0)              # wall clock stepped back
    assert b > a


def test_hybrid_clock_moves_past_observed_stamp():
    h = M.HybridClock()
    h.observe((500.0, 3), physical_ms=100.0)   # a remote stamp from a clock that is ahead
    assert h.now(101.0) > (500.0, 3)


def test_lww_drops_one_of_two_concurrent_preference_edits():
    ws = [W(0, "east", 10, "pref", "language", "en", {"east": 1}),
          W(1, "west", 12, "pref", "language", "fr", {"west": 1})]
    assert M.lww(ws, "wall")["dropped"] == 1


def test_version_vectors_keep_both_siblings():
    ws = [W(0, "east", 10, "pref", "language", "en", {"east": 1}),
          W(1, "west", 12, "pref", "language", "fr", {"west": 1})]
    v = M.version_vectors(ws)
    assert v["siblings"] == 2 and v["conflict_flagged"] and v["dropped"] == 0


def test_custom_merge_unions_memory_facts():
    ws = [W(0, "east", 10, "fact", "likes trains", "saved", {"east": 1}),
          W(1, "west", 12, "fact", "allergic to nuts", "saved", {"west": 1})]
    assert M.lww(ws, "wall")["dropped"] == 1
    assert M.custom_merge(ws) == {"dropped": 0, "fields_flagged": 0}


def test_custom_merge_keeps_different_fields():
    ws = [W(0, "east", 10, "pref", "language", "en", {"east": 1}),
          W(1, "west", 12, "pref", "units", "metric", {"west": 1})]
    assert M.lww(ws, "wall")["dropped"] == 1 and M.custom_merge(ws)["dropped"] == 0


def test_custom_merge_flags_same_field_clash():
    ws = [W(0, "east", 10, "pref", "tone", "formal", {"east": 1}),
          W(1, "west", 12, "pref", "tone", "casual", {"west": 1})]
    assert M.custom_merge(ws)["fields_flagged"] == 1


def test_same_value_concurrent_edit_is_not_a_clash():
    ws = [W(0, "east", 10, "pref", "tone", "formal", {"east": 1}),
          W(1, "west", 12, "pref", "tone", "formal", {"west": 1})]
    assert M.custom_merge(ws)["fields_flagged"] == 0


def test_causal_overwrite_is_not_counted_as_dropped():
    ws = [W(0, "east", 10, "pref", "tone", "formal", {"east": 1}),
          W(1, "west", 500, "pref", "tone", "casual", {"east": 1, "west": 1})]
    assert M.lww(ws, "wall")["dropped"] == 0


def test_skewed_wall_clock_drops_a_causally_later_edit():
    # west saw east's edit, then edited; west's clock is 1 s behind so its stamp looks older
    ws = [W(0, "east", 1000, "pref", "tone", "formal", {"east": 1}, wall=(1000, "east"), hlc=(1000, 0, "east")),
          W(1, "west", 1200, "pref", "tone", "casual", {"east": 1, "west": 1}, wall=(200, "west"),
            hlc=(1000, 1, "west"))]
    a, b = M.lww(ws, "wall"), M.lww(ws, "hlc")
    assert a["dropped_wrong_order"] == 1 and b["dropped"] == 0


def test_make_record_writes_tracks_causality():
    ws = M.make_record_writes(random.Random(1), 8, 2000, 120, 0)
    for w in ws:
        for x in ws:
            if x.region == w.region and x.t < w.t:
                assert M.vv_leq(x.vv, w.vv)       # a region always sees its own earlier writes


def test_hlc_stamps_respect_causality_under_skew():
    ws = M.make_record_writes(random.Random(2), 10, 3000, 100, 2000)
    for w in ws:
        for x in ws:
            if x.wid != w.wid and M.vv_leq(x.vv, w.vv):
                assert x.hlc_ts[:2] < w.hlc_ts[:2]


def test_no_skew_no_wrong_order_drops():
    r = M.conflict_run(records=300, skew_ms=0)
    assert r["lww_wall_wrong_order"] == 0 and r["lww_wall_dropped"] > 0


def test_wrong_order_drops_grow_with_skew():
    s = M.skew_sweep(skews=(0, 200, 2000), records=300)
    w = [x["lww_wall_wrong_order"] for x in s]
    assert w[0] == 0 < w[1] < w[2]


def test_hlc_has_no_wrong_order_drops_at_any_skew():
    assert all(x["lww_hlc_wrong_order"] == 0 for x in M.skew_sweep(skews=(0, 500, 2000), records=300))


def test_merge_and_version_vectors_drop_nothing():
    r = M.conflict_run(records=500, skew_ms=500)
    assert r["merge_dropped"] == 0 and r["vv_dropped"] == 0 and r["vv_records_flagged"] > 0


def test_hybrid_latency_order():
    h = {m: M.hybrid_run(m, writes=1500) for m in M.HYBRID_MODES}
    assert h["all-async"]["write_p50_ms"] < h["local-sync+remote-async"]["write_p50_ms"] < h["all-sync"]["write_p50_ms"]


def test_hybrid_protects_against_node_loss_not_region_loss():
    h = M.hybrid_run("local-sync+remote-async", writes=1500)
    assert h["node_failure_acked_lost_mean"] == 0 and h["region_failure_acked_lost_mean"] > 0


def test_all_sync_loses_nothing_on_region_loss():
    assert M.hybrid_run("all-sync", writes=1500)["region_failure_acked_lost_mean"] == 0


def test_all_async_at_risk_on_node_loss():
    assert M.hybrid_run("all-async", writes=1500)["node_failure_acked_lost_mean"] > 0


def test_unknown_hybrid_mode_rejected():
    with pytest.raises(ValueError):
        M.hybrid_run("quorum")


def test_committed_results_match_a_fresh_run():
    out = subprocess.run([sys.executable, "scripts/replication_bench.py", "--check"], cwd=ROOT, capture_output=True,
                         text=True)
    assert out.returncode == 0, out.stdout


def test_board_numbers_match_results():
    out = subprocess.run([sys.executable, "scripts/replication_board.py", "--check"], cwd=ROOT, capture_output=True,
                         text=True)
    assert out.returncode == 0, out.stdout
    assert (ROOT / "docs/img/replication_board.png").stat().st_size > 50_000


def test_results_labelled_simulated_and_doc_has_built_vs_planned():
    r = json.loads((ROOT / "docs/results/replication.json").read_text())
    assert "simulated" in r["primary_replica"]["note"] and "simulated" in r["multi_region"]["note"]
    doc = (ROOT / "docs/replication.md").read_text()
    assert "Built vs planned" in doc and "```mermaid" in doc
