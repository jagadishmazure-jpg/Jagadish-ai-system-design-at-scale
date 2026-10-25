"""Primary-replica replication: log shipping, commit modes, read-your-writes, failover, bootstrap,
leaderless quorums and outbox CDC. All simulated with fixed seeds."""
import pytest

from sysdesign.cache import SemanticCache
from sysdesign.ext import replication as R


# ---------------------------------------------------------------- log shipping
def test_primary_assigns_increasing_positions():
    p = R.Primary()
    lsns = [p.write("k", i).lsn for i in range(5)]
    assert lsns == [1, 2, 3, 4, 5] and p.lsn == 5


def test_pull_copies_only_new_entries_into_relay_log():
    p, r = R.Primary(), R.Replica("r")
    for i in range(3):
        p.write(f"k{i}", i)
    assert r.pull(p) == 3 and r.pull(p) == 0
    p.write("k9", 9)
    assert r.pull(p) == 1 and len(r.relay) == 4


def test_apply_replays_in_order_and_last_write_wins_per_key():
    p, r = R.Primary(), R.Replica("r")
    p.write("a", 1)
    p.write("a", 2)
    r.pull(p)
    assert r.apply() == 2 and r.data == {"a": 2} and r.applied_lsn == 2


def test_apply_limit_leaves_lag():
    p, r = R.Primary(), R.Replica("r")
    for i in range(10):
        p.write("k", i)
    r.pull(p)
    r.apply(limit=4)
    assert r.applied_lsn == 4 and r.lag == 6


def test_reapplying_old_entries_is_a_no_op():
    p, r = R.Primary(), R.Replica("r")
    p.write("a", 1)
    r.pull(p)
    r.apply()
    r.relay.append(R.LogEntry(1, "a", "OLD"))
    assert r.apply() == 0 and r.data["a"] == 1


def test_pull_upto_stops_at_position():
    p, r = R.Primary(), R.Replica("r")
    for i in range(6):
        p.write("k", i)
    assert r.pull(p, upto=3) == 3


def test_log_shipping_demo_replicas_converge():
    d = R.log_shipping_demo()
    assert d["replicas_match_primary_after_drain"] and d["max_lag_entries"]["r-slow"] > d["max_lag_entries"]["r-fast"]


# ---------------------------------------------------------------- commit modes
def test_commit_wait_by_mode():
    acks = [3.0, 1.0, 2.0]
    assert R.commit_wait_ms("sync", acks) == 3.0
    assert R.commit_wait_ms("semi-sync", acks) == 1.0
    assert R.commit_wait_ms("async", acks) == 0.0


def test_unknown_mode_rejected():
    with pytest.raises(ValueError):
        R.commit_wait_ms("eventual", [1.0])


def test_latency_order_sync_slowest_async_fastest():
    m = {k: R.mode_run(k, writes=1000) for k in R.MODES}
    assert m["async"]["write_p50_ms"] < m["semi-sync"]["write_p50_ms"] < m["sync"]["write_p50_ms"]
    assert m["semi-sync"]["write_p95_ms"] < m["sync"]["write_p95_ms"]


@pytest.mark.parametrize("mode", ["sync", "semi-sync"])
def test_sync_modes_never_lose_acked_writes(mode):
    assert R.crash_loss_trials(mode, trials=50)["acked_writes_lost_total"] == 0


def test_async_loses_acked_writes_on_crash():
    c = R.crash_loss_trials("async", trials=50)
    assert c["acked_writes_lost_total"] > 0 and c["trials_with_loss"] > 0


def test_mode_run_is_deterministic():
    assert R.mode_run("async", writes=300) == R.mode_run("async", writes=300)


# ---------------------------------------------------------------- read-your-writes
def test_replica_only_reads_are_often_stale():
    assert R.ryw_run("replica")["stale_read_rate"] > 0.3


def test_lsn_token_gives_zero_stale_reads():
    assert R.ryw_run("lsn_token")["stale_reads"] == 0


def test_lsn_token_uses_primary_less_than_time_window():
    t, w = R.ryw_run("lsn_token"), R.ryw_run("primary_window")
    assert t["primary_read_share"] < w["primary_read_share"]


def test_unknown_ryw_policy_rejected():
    with pytest.raises(ValueError):
        R.ryw_run("random")


def test_router_sends_fresh_read_to_primary_until_replica_catches_up():
    p, r = R.Primary(), R.Replica("r1")
    router = R.ReadRouter(p, [r])
    router.write("s1", "conv:42", "turn saved")
    assert router.read("s1", "conv:42") == ("turn saved", "primary")
    r.pull(p)
    r.apply()
    assert router.read("s1", "conv:42") == ("turn saved", "r1")


def test_router_other_sessions_can_use_lagging_replica():
    p, r = R.Primary(), R.Replica("r1")
    router = R.ReadRouter(p, [r])
    router.write("s1", "feedback:7", "thumbs down")
    assert router.read("s2", "feedback:7") == (None, "r1")   # s2 never wrote: replica is acceptable


# ---------------------------------------------------------------- failover
def test_promote_picks_most_caught_up_replica_and_bumps_epoch():
    c = R.Cluster(R.Primary(), [R.Replica("r1", applied_lsn=5), R.Replica("r2", applied_lsn=9)])
    assert c.promote_most_caught_up().name == "r2" and c.epoch == 2


def test_fence_rejects_old_primary_writes():
    c = R.Cluster(R.Primary(), [R.Replica("r1")])
    c.promote_most_caught_up()
    with pytest.raises(R.FencedError):
        c.old_primary_write(writer_epoch=1)


def test_failover_without_fence_accepts_split_brain_writes():
    d = R.failover_drill("async", fencing=False)
    assert d["split_brain_writes_accepted"] == d["late_writes_to_old_primary"] > 0


def test_failover_with_fence_accepts_none():
    d = R.failover_drill("async")
    assert d["split_brain_writes_accepted"] == 0 and d["split_brain_writes_rejected"] > 0


def test_failover_unavailability_is_detect_plus_promote():
    d = R.failover_drill("sync", promote_ms=800.0)
    assert d["writes_unavailable_ms"] == pytest.approx(d["detect_ms"] + 800.0, abs=0.2)
    assert 1000.0 <= d["detect_ms"] <= 1500.0      # 3 missed 500 ms heartbeats


def test_failover_loss_only_in_async():
    assert R.failover_drill("semi-sync")["acked_writes_lost"] == 0
    assert R.failover_drill("async")["acked_writes_lost"] > 0


# ---------------------------------------------------------------- bootstrap
def test_snapshot_bootstrap_matches_primary():
    b = R.bootstrap_real()
    assert b["matches_primary"] and b["full_matches_primary"]


def test_snapshot_bootstrap_pulls_only_the_tail():
    b = R.bootstrap_real(rows=1000, tail_writes=200)
    assert b["tail_entries_pulled"] == 200 and b["full_entries_pulled"] == 1200


def test_snapshot_faster_and_lighter_on_primary_than_full_copy():
    b = R.bootstrap_compare()
    assert b["snapshot_plus_tail_s"] < b["full_copy_s"] and b["primary_read_reduction"] > 0.9


def test_old_snapshot_can_lose_to_full_copy():
    # a very stale snapshot on a small table: replaying the tail costs more than copying (known limit)
    b = R.bootstrap_compare(rows=100_000, snapshot_age_s=3600)
    assert b["snapshot_plus_tail_s"] > b["full_copy_s"]


# ---------------------------------------------------------------- leaderless
@pytest.mark.parametrize("w,r", [(2, 2), (3, 1), (1, 3)])
def test_overlapping_quorums_never_read_stale(w, r):
    assert R.leaderless_run(w=w, r=r, ops=2000)["stale_reads"] == 0


@pytest.mark.parametrize("w,r", [(1, 1), (1, 2), (2, 1)])
def test_non_overlapping_quorums_read_stale(w, r):
    assert R.leaderless_run(w=w, r=r, ops=2000)["stale_reads"] > 0


def test_read_repair_reduces_copies_behind():
    off = R.leaderless_run(w=1, r=2, read_repair=False)
    on = R.leaderless_run(w=1, r=2, read_repair=True)
    assert on["node_copies_behind_at_end"] < off["node_copies_behind_at_end"]
    assert on["stale_reads"] < off["stale_reads"]


def test_all_node_writes_leave_nothing_behind():
    assert R.leaderless_run(w=3, r=1)["node_copies_behind_at_end"] == 0


# ---------------------------------------------------------------- CDC outbox
def test_outbox_row_commits_with_data():
    s = R.OutboxStore()
    s.save("d1", "a\n\nb", 1.0)
    assert s.docs() == {"d1": "a\n\nb"}
    assert s.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1


def test_crash_inside_transaction_leaves_neither_row():
    s = R.OutboxStore()
    with pytest.raises(R.CrashError):
        s.save("d1", "a", 1.0, crash_after_data=True)
    assert s.docs() == {} and s.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0


def test_consumer_applies_upserts_and_deletes_to_index():
    s, idx = R.OutboxStore(), R.empty_index()
    s.save("d1", "refunds in 30 days\n\nreturns need a receipt", 0)
    s.save("d2", "shipping is free", 1)
    s.save("d1", None, 2)
    R.CdcConsumer(s, idx).poll()
    assert {c.doc_id for c in idx.chunks.values()} == {"d2"} and R.index_mismatches(s, idx) == 0


def test_consumer_resumes_from_saved_position_after_crash():
    s, idx = R.OutboxStore(), R.empty_index()
    for i in range(5):
        s.save(f"d{i}", f"body {i}", i)
    c = R.CdcConsumer(s, idx)
    with pytest.raises(R.CrashError):
        c.poll(crash_before_offset=True)
    assert c.position() == 0
    embeds = idx.embed_calls
    c.poll()
    assert c.position() == 5 and c.replayed_events == 5 and idx.embed_calls == embeds   # replay re-embeds nothing


def test_consumer_invalidates_dependent_cache_entries():
    s, idx, cache = R.OutboxStore(), R.empty_index(), R.ChunkCache()
    s.save("d1", "old rule", 0)
    c = R.CdcConsumer(s, idx, cache)
    c.poll()
    cache.put("what is the rule", {"d1#0"})
    cache.put("unrelated", {"d9#0"})
    s.save("d1", "new rule", 1)
    c.poll()
    assert set(cache.entries) == {"unrelated"}


def test_consumer_works_with_the_real_semantic_cache():
    fakeredis = pytest.importorskip("fakeredis")
    s, idx = R.OutboxStore(), R.empty_index()
    cache = SemanticCache(fakeredis.FakeRedis())
    s.save("d1", "refunds take 30 days", 0)
    c = R.CdcConsumer(s, idx, cache)
    c.poll()
    cache.put("t", ["read"], "policy", "how long do refunds take", "30 days", ["d1#0"])
    assert cache.size() == 1
    s.save("d1", "refunds take 14 days", 1)
    c.poll()
    assert cache.size() == 0


def test_cdc_run_index_matches_db_despite_crashes():
    d = R.cdc_run(events=400)
    assert d["consumer_crashes"] > 0 and d["events_replayed"] > 0 and d["index_docs_mismatched"] == 0


def test_cdc_staleness_bounded_by_poll_interval_plus_batch():
    d = R.cdc_run(events=400, poll_ms=100.0)
    assert d["staleness_p95_ms"] <= 100.0 + 50 * 2.0 and d["staleness_p50_ms"] > 0


def test_faster_polling_cuts_staleness():
    assert R.cdc_run(events=300, poll_ms=50.0)["staleness_p95_ms"] < R.cdc_run(events=300, poll_ms=400.0)["staleness_p95_ms"]


def test_results_have_every_section():
    r = R.results()
    assert set(r) >= {"note", "modes", "crash_trials", "read_your_writes", "failover", "bootstrap", "leaderless", "cdc"}
    assert "simulated" in r["note"]
