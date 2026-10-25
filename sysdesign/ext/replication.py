"""Primary-replica replication, simulated end to end with fixed seeds.

Everything here is a simulation: times are simulated milliseconds drawn from seeded random
generators, not measurements of a real database. Counts (writes lost, stale reads, rows copied)
are exact counts from those runs. The data structures are real Python objects, so the tests can
check that a replica really ends up with the same rows as the primary.

Pieces (one per section below):
1. A primary with an ordered change log; replicas pull entries into a relay log (the I/O step)
   and apply them (the apply step), then acknowledge. Sync, semi-sync and async commit modes.
2. Read-your-writes routing: a session's last write position decides whether a replica is fresh
   enough to serve its read, otherwise the read goes to the primary.
3. A failover drill: heartbeats, promotion of the most caught-up replica, and an epoch fence so
   the old primary cannot keep accepting writes after it comes back.
4. Bootstrapping a new replica from a snapshot of an existing replica plus the log tail, compared
   with a full copy from the primary.
5. Leaderless replication with N nodes and W/R quorums, with and without read repair.
6. Change data capture: an outbox row written in the same SQLite transaction as the data, and a
   consumer that keeps a vector index and a cache fresh, saves its position and resumes.
Two-region multi-primary, conflict handling and clocks live in ``multiregion.py``.
"""
from __future__ import annotations

import random
import sqlite3
from dataclasses import dataclass, field

from sysdesign.observability import percentile

NOTE = "simulated (fixed seeds); times are simulated ms, counts are exact counts from the runs"


# ---------------------------------------------------------------- 1. change log, replicas, modes
@dataclass(frozen=True)
class LogEntry:
    lsn: int          # position in the primary's change log
    key: str
    value: object
    epoch: int = 1


@dataclass
class Replica:
    name: str
    relay: list[LogEntry] = field(default_factory=list)
    data: dict = field(default_factory=dict)
    applied_lsn: int = 0

    def pull(self, primary: Primary, upto: int | None = None) -> int:
        """I/O step: copy new change-log entries into the relay log. Returns entries copied."""
        start = self.relay[-1].lsn if self.relay else self.applied_lsn
        new = [e for e in primary.log if e.lsn > start and (upto is None or e.lsn <= upto)]
        self.relay.extend(new)
        return len(new)

    def apply(self, limit: int | None = None) -> int:
        """Apply step: replay relay-log entries in order. Returns entries applied."""
        n = 0
        while self.relay and (limit is None or n < limit):
            e = self.relay.pop(0)
            if e.lsn <= self.applied_lsn:
                continue          # already applied (idempotent replay)
            self.data[e.key] = e.value
            self.applied_lsn = e.lsn
            n += 1
        return n

    @property
    def lag(self) -> int:
        return 0 if not self.relay else self.relay[-1].lsn - self.applied_lsn


@dataclass
class Primary:
    data: dict = field(default_factory=dict)
    log: list[LogEntry] = field(default_factory=list)
    epoch: int = 1

    @property
    def lsn(self) -> int:
        return self.log[-1].lsn if self.log else 0

    def write(self, key: str, value) -> LogEntry:
        e = LogEntry(self.lsn + 1, key, value, self.epoch)
        self.log.append(e)
        self.data[key] = value
        return e


MODES = ("sync", "semi-sync", "async")


def commit_wait_ms(mode: str, replica_ack_ms: list[float]) -> float:
    """Extra time a client waits after the local commit, given each replica's ack delay."""
    if mode == "sync":
        return max(replica_ack_ms)
    if mode == "semi-sync":
        return min(replica_ack_ms)
    if mode == "async":
        return 0.0
    raise ValueError(f"unknown mode {mode!r}")


def mode_run(mode: str, writes: int = 4000, replicas: int = 2, rate_per_s: float = 400.0,
             local_commit_ms: float = 0.4, net_ms: float = 2.0, apply_ms: float = 0.3, seed: int = 7) -> dict:
    """Simulate a write stream, then crash the primary at the end of it.

    Each replica's copy of a write lands after a network delay plus apply time (exponential tail).
    An acknowledged write is lost if, at the crash, no replica holds it. Sync and semi-sync never
    acknowledge before a replica has the entry, so their loss is 0 by construction; the run shows it.
    """
    rng = random.Random(seed)
    t, lat, acked, arrive = 0.0, [], [], []
    for _ in range(writes):
        t += rng.expovariate(rate_per_s / 1000.0)
        ack = [net_ms * (0.5 + rng.expovariate(1.0)) + apply_ms for _ in range(replicas)]
        wait = commit_wait_ms(mode, ack)
        lat.append(local_commit_ms + wait)
        acked.append(t + local_commit_ms + wait)
        arrive.append(min(t + local_commit_ms + a for a in ack))
    crash = t + local_commit_ms           # primary dies right after the last local commit
    acked_by_crash = [i for i, a in enumerate(acked) if a <= crash]
    lost = sum(1 for i in acked_by_crash if arrive[i] > crash)
    return {"mode": mode, "writes": writes, "replicas": replicas,
            "write_p50_ms": round(percentile(lat, 50), 3), "write_p95_ms": round(percentile(lat, 95), 3),
            "acked_before_crash": len(acked_by_crash), "acked_writes_lost_on_crash": lost}


def crash_loss_trials(mode: str, trials: int = 200, seed: int = 70) -> dict:
    """Repeat the crash at a random point in a busy stream; count acknowledged writes lost."""
    rng = random.Random(seed)
    lost, any_loss = 0, 0
    for _ in range(trials):
        r = mode_run(mode, writes=rng.randint(50, 400), rate_per_s=2000.0, seed=rng.randrange(10**9))
        lost += r["acked_writes_lost_on_crash"]
        any_loss += r["acked_writes_lost_on_crash"] > 0
    return {"mode": mode, "trials": trials, "acked_writes_lost_total": lost,
            "trials_with_loss": any_loss, "mean_lost_per_crash": round(lost / trials, 3)}


def log_shipping_demo(writes: int = 500, seed: int = 3) -> dict:
    """Run the real objects: writes, two replicas pulling and applying at different speeds."""
    rng = random.Random(seed)
    p = Primary()
    fast, slow = Replica("r-fast"), Replica("r-slow")
    max_lag = {"r-fast": 0, "r-slow": 0}
    for i in range(writes):
        p.write(f"k{rng.randrange(200)}", i)
        fast.pull(p)
        fast.apply()
        if i % 5 == 0:
            slow.pull(p)
        slow.apply(limit=1)
        for r in (fast, slow):
            max_lag[r.name] = max(max_lag[r.name], p.lsn - r.applied_lsn)
    for r in (fast, slow):
        r.pull(p)
        r.apply()
    return {"writes": writes, "max_lag_entries": max_lag,
            "replicas_match_primary_after_drain": fast.data == p.data and slow.data == p.data}


# ---------------------------------------------------------------- 2. read-your-writes
def ryw_run(policy: str, sessions: int = 2000, seed: int = 11, think_ms_mean: float = 30.0,
            lag_ms_mean: float = 40.0, replicas: int = 2) -> dict:
    """A user saves a conversation turn or a feedback rating, then reads it back.

    policy 'replica': every read goes to a random replica (stale if that replica has not applied it).
    policy 'primary_window': reads within window_ms of the session's last write go to the primary.
    policy 'lsn_token': the session carries the position of its last write; a replica serves the read
    only if it has applied at least that position, otherwise the primary does.
    """
    rng = random.Random(seed)
    stale = to_primary = 0
    window_ms = 100.0
    for _ in range(sessions):
        lags = [rng.expovariate(1 / lag_ms_mean) for _ in range(replicas)]
        think = rng.expovariate(1 / think_ms_mean)
        pick = rng.randrange(replicas)
        if policy == "replica":
            stale += lags[pick] > think
        elif policy == "primary_window":
            if think <= window_ms:
                to_primary += 1
            else:
                stale += lags[pick] > think
        elif policy == "lsn_token":
            fresh = [i for i, lag in enumerate(lags) if lag <= think]
            if not fresh:
                to_primary += 1
        else:
            raise ValueError(policy)
    return {"policy": policy, "sessions": sessions, "stale_reads": stale,
            "stale_read_rate": round(stale / sessions, 4), "reads_on_primary": to_primary,
            "primary_read_share": round(to_primary / sessions, 4)}


@dataclass
class ReadRouter:
    """Real router over Primary/Replica objects using a per-session write position."""
    primary: Primary
    replicas: list[Replica]
    session_lsn: dict = field(default_factory=dict)

    def write(self, session: str, key: str, value) -> int:
        e = self.primary.write(key, value)
        self.session_lsn[session] = e.lsn
        return e.lsn

    def read(self, session: str, key: str):
        need = self.session_lsn.get(session, 0)
        for r in self.replicas:
            if r.applied_lsn >= need:
                return r.data.get(key), r.name
        return self.primary.data.get(key), "primary"


# ---------------------------------------------------------------- 3. failover drill
class FencedError(Exception):
    pass


@dataclass
class Cluster:
    """Nodes accept writes only with the current epoch; promotion bumps the epoch (the fence)."""
    primary: Primary
    replicas: list[Replica]
    epoch: int = 1
    fencing: bool = True
    rejected_stale_writes: int = 0
    accepted_stale_writes: int = 0

    def promote_most_caught_up(self) -> Replica:
        best = max(self.replicas, key=lambda r: (r.applied_lsn, r.name))
        self.epoch += 1
        return best

    def old_primary_write(self, writer_epoch: int) -> None:
        """A write arriving at the old primary after it comes back."""
        if self.fencing and writer_epoch < self.epoch:
            self.rejected_stale_writes += 1
            raise FencedError("stale epoch: this node is no longer primary")
        self.accepted_stale_writes += 1


def failover_drill(mode: str, seed: int = 21, heartbeat_ms: float = 500.0, misses: int = 3,
                   promote_ms: float = 800.0, fencing: bool = True) -> dict:
    """Primary dies mid-stream; detect via missed heartbeats, promote, fence, count what was lost."""
    rng = random.Random(seed)
    m = mode_run(mode, writes=600, rate_per_s=1500.0, seed=seed)
    phase = rng.uniform(0, heartbeat_ms)              # when in the heartbeat cycle the crash lands
    detect = (heartbeat_ms - phase) + heartbeat_ms * (misses - 1)
    unavailable = detect + promote_ms
    # the old primary comes back still believing it is primary; clients that cached it keep writing
    c = Cluster(Primary(), [Replica("r1"), Replica("r2")], fencing=fencing)
    c.promote_most_caught_up()
    late = rng.randint(5, 15)
    for _ in range(late):
        try:
            c.old_primary_write(writer_epoch=1)
        except FencedError:
            pass
    return {"mode": mode, "detect_ms": round(detect, 1), "writes_unavailable_ms": round(unavailable, 1),
            "acked_writes_lost": m["acked_writes_lost_on_crash"], "late_writes_to_old_primary": late,
            "split_brain_writes_accepted": c.accepted_stale_writes,
            "split_brain_writes_rejected": c.rejected_stale_writes, "fencing": fencing}


# ---------------------------------------------------------------- 4. bootstrap a new replica
def bootstrap_compare(rows: int = 20_000_000, row_bytes: int = 1024, write_rate_per_s: float = 500.0,
                      snapshot_age_s: float = 300.0, copy_mb_s: float = 50.0, apply_rows_s: float = 20_000.0,
                      snapshot_restore_mb_s: float = 400.0) -> dict:
    """Simulated catch-up time and primary load: full copy from the primary vs snapshot + log tail.

    Full copy: stream every row from the primary, then replay the writes that arrived during the
    copy (repeat until caught up). Snapshot: restore an existing replica's snapshot (taken
    snapshot_age_s ago at a known log position), then replay only the log tail since that position.
    """
    size_mb = rows * row_bytes / 1e6
    # full copy: copy, then chase the writes that landed meanwhile
    t = size_mb / copy_mb_s
    behind = write_rate_per_s * t
    chase, rounds = 0.0, 0
    while behind > 1 and rounds < 50:
        dt = behind / apply_rows_s
        chase += dt
        behind = write_rate_per_s * dt
        rounds += 1
    full = t + chase
    # snapshot + tail
    restore = size_mb / snapshot_restore_mb_s
    tail = write_rate_per_s * (snapshot_age_s + restore)
    t2 = tail / apply_rows_s
    behind = write_rate_per_s * t2
    while behind > 1:
        dt = behind / apply_rows_s
        t2 += dt
        behind = write_rate_per_s * dt
    snap = restore + t2
    return {"rows": rows, "size_mb": round(size_mb, 1), "full_copy_s": round(full, 2),
            "snapshot_plus_tail_s": round(snap, 2), "speedup": round(full / snap, 2),
            "rows_read_from_primary_full": rows + int(write_rate_per_s * full),
            "log_entries_read_from_primary_snapshot": int(tail),
            "primary_read_reduction": round(1 - int(tail) / (rows + int(write_rate_per_s * full)), 4)}


def bootstrap_real(rows: int = 3000, tail_writes: int = 800, seed: int = 5) -> dict:
    """Do it with the real objects: snapshot an existing replica, replay the tail, compare contents."""
    rng = random.Random(seed)
    p, donor = Primary(), Replica("donor")
    for i in range(rows):
        p.write(f"k{i}", i)
    donor.pull(p)
    donor.apply()
    snap_data, snap_lsn = dict(donor.data), donor.applied_lsn
    for i in range(tail_writes):
        p.write(f"k{rng.randrange(rows + 500)}", -i)
    new = Replica("new", data=snap_data, applied_lsn=snap_lsn)
    copied = new.pull(p)
    new.apply()
    full = Replica("full")
    full_copied = full.pull(p)
    full.apply()
    return {"snapshot_lsn": snap_lsn, "tail_entries_pulled": copied, "full_entries_pulled": full_copied,
            "matches_primary": new.data == p.data, "full_matches_primary": full.data == p.data}


# ---------------------------------------------------------------- 5. leaderless quorums
def leaderless_run(n: int = 3, w: int = 2, r: int = 2, read_repair: bool = False, keys: int = 300,
                   ops: int = 6000, async_miss: float = 0.5, seed: int = 31) -> dict:
    """N nodes hold versioned values. A write is acknowledged after W nodes store it; each other node
    misses it with probability async_miss (it was down or the copy was dropped). A read asks R random
    nodes and returns the highest version. Read repair writes that version back to stale nodes it asked."""
    rng = random.Random(seed)
    nodes = [dict() for _ in range(n)]
    latest: dict[str, int] = {}
    stale = reads = 0
    for _ in range(ops):
        k = f"k{rng.randrange(keys)}"
        if rng.random() < 0.4:
            v = latest.get(k, 0) + 1
            latest[k] = v
            order = rng.sample(range(n), n)
            for i, node in enumerate(order):
                if i < w or rng.random() > async_miss:
                    nodes[node][k] = v
        elif k in latest:
            reads += 1
            asked = rng.sample(range(n), r)
            got = max(nodes[i].get(k, 0) for i in asked)
            stale += got < latest[k]
            if read_repair:
                for i in asked:
                    if nodes[i].get(k, 0) < got:
                        nodes[i][k] = got
    behind = sum(1 for k, v in latest.items() for nd in nodes if nd.get(k, 0) < v)
    return {"n": n, "w": w, "r": r, "overlap": w + r > n, "read_repair": read_repair, "reads": reads,
            "stale_reads": stale, "stale_read_rate": round(stale / max(reads, 1), 4),
            "node_copies_behind_at_end": behind}


# ---------------------------------------------------------------- 6. change data capture (outbox)
SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (doc_id TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, doc_id TEXT NOT NULL,
                                   op TEXT NOT NULL, body TEXT, committed_ms REAL NOT NULL);
CREATE TABLE IF NOT EXISTS consumer_offset (name TEXT PRIMARY KEY, last_id INTEGER NOT NULL);
"""


class CrashError(Exception):
    pass


class OutboxStore:
    """Data and its change event commit in ONE transaction, so neither exists without the other."""

    def __init__(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.executescript(SCHEMA)

    def save(self, doc_id: str, body: str | None, now_ms: float, crash_after_data: bool = False) -> None:
        with self.db:      # one transaction: rolls back both rows on any exception
            if body is None:
                self.db.execute("DELETE FROM docs WHERE doc_id=?", (doc_id,))
            else:
                self.db.execute("INSERT INTO docs VALUES (?, ?) ON CONFLICT(doc_id) DO UPDATE SET body=excluded.body",
                                (doc_id, body))
            if crash_after_data:
                raise CrashError("process died before the outbox row")
            self.db.execute("INSERT INTO outbox (doc_id, op, body, committed_ms) VALUES (?, ?, ?, ?)",
                            (doc_id, "delete" if body is None else "upsert", body, now_ms))

    def docs(self) -> dict:
        return dict(self.db.execute("SELECT doc_id, body FROM docs"))


class ChunkCache:
    """Minimal answer cache keyed by the chunk ids an answer was built from."""

    def __init__(self) -> None:
        self.entries: dict[str, set[str]] = {}

    def put(self, q: str, chunk_ids: set[str]) -> None:
        self.entries[q] = set(chunk_ids)

    def invalidate_chunks(self, chunk_ids: set[str]) -> int:
        gone = [q for q, ids in self.entries.items() if ids & chunk_ids]
        for q in gone:
            del self.entries[q]
        return len(gone)


class CdcConsumer:
    """Reads the outbox in id order, updates the index and cache, then saves its position.

    If it dies after applying a batch but before saving the position, the batch is replayed on
    restart. The index upsert is keyed by content hash, so a replay changes nothing (no double apply).
    """

    def __init__(self, store: OutboxStore, index, cache=None, name: str = "vector-index") -> None:
        self.store, self.index, self.cache, self.name = store, index, cache, name
        self.applied_effects = 0
        self.replayed_events = 0
        self._seen: set[int] = set()

    def position(self) -> int:
        row = self.store.db.execute("SELECT last_id FROM consumer_offset WHERE name=?", (self.name,)).fetchone()
        return row[0] if row else 0

    def poll(self, batch: int = 50, crash_before_offset: bool = False) -> list[tuple[int, float]]:
        rows = self.store.db.execute("SELECT id, doc_id, op, body, committed_ms FROM outbox WHERE id>? "
                                     "ORDER BY id LIMIT ?", (self.position(), batch)).fetchall()
        done = []
        for oid, doc_id, op, body, committed in rows:
            if oid in self._seen:
                self.replayed_events += 1
            self._seen.add(oid)
            paras = [] if op == "delete" else [p for p in body.split("\n\n") if p.strip()]
            changed = self.index.apply_change(doc_id, paras)
            self.applied_effects += len(changed)
            if self.cache is not None and changed:
                self.cache.invalidate_chunks(changed)
            done.append((oid, committed))
        if crash_before_offset and rows:
            raise CrashError("consumer died before saving its position")
        if rows:
            with self.store.db:
                self.store.db.execute("INSERT INTO consumer_offset VALUES (?, ?) ON CONFLICT(name) "
                                      "DO UPDATE SET last_id=excluded.last_id", (self.name, rows[-1][0]))
        return done


def empty_index():
    """A KnowledgeIndex with no documents (its constructor loads sample docs when given none)."""
    from sysdesign.knowledge import KnowledgeIndex

    idx = KnowledgeIndex()
    idx.chunks.clear()
    idx.embed_calls = 0
    return idx


def index_mismatches(store: OutboxStore, index) -> int:
    """Documents whose indexed paragraphs differ from the database row (missing, extra or stale)."""
    db = {d: [p for p in b.split("\n\n") if p.strip()] for d, b in store.docs().items()}
    idx: dict[str, list] = {}
    for c in sorted(index.chunks.values(), key=lambda c: int(c.chunk_id.rsplit("#", 1)[1])):
        idx.setdefault(c.doc_id, []).append(c.text)
    return sum(db.get(d) != idx.get(d) for d in set(db) | set(idx))


def cdc_run(events: int = 1500, rate_per_s: float = 50.0, poll_ms: float = 200.0, per_event_ms: float = 2.0,
            crash_every: int = 7, seed: int = 41) -> dict:
    """Simulated clock, real SQLite outbox and real KnowledgeIndex.

    Staleness = simulated time from commit until the index reflects the change.
    Every crash_every-th poll the consumer dies after applying and before saving its position.
    A dual-write baseline (save the doc, then separately update the index) loses the index update
    whenever the process dies between the two steps; the same crash points are used.
    """
    rng = random.Random(seed)
    store = OutboxStore()
    index = empty_index()
    cache = ChunkCache()
    consumer = CdcConsumer(store, index, cache)
    t, next_poll, polls, crashes, stale_ms = 0.0, poll_ms, 0, 0, []
    dual_lost = 0
    commits = 0
    for i in range(events):
        t += rng.expovariate(rate_per_s / 1000.0)
        while next_poll <= t:
            polls += 1
            crash = crash_every and polls % crash_every == 0
            try:
                done = consumer.poll(crash_before_offset=bool(crash))
            except CrashError:
                crashes += 1
                done = consumer.poll()          # restart: replays from the saved position
            for k, (_, committed) in enumerate(done):
                stale_ms.append(next_poll + (k + 1) * per_event_ms - committed)
            next_poll += poll_ms
        doc = f"policy-{rng.randrange(120)}"
        body = None if rng.random() < 0.05 else f"Version {i} of {doc}.\n\nClause {rng.randrange(9)} applies."
        if rng.random() < 0.02:                  # app dies inside the transaction: nothing commits
            try:
                store.save(doc, body, t, crash_after_data=True)
            except CrashError:
                dual_lost += 1                   # with dual writes the doc would be saved, index never told
            continue
        store.save(doc, body, t)
        commits += 1
        cache.put(f"q-{doc}", {f"{doc}#0"})
    while True:                                  # drain
        done = consumer.poll()
        if not done:
            break
        for k, (_, committed) in enumerate(done):
            stale_ms.append(next_poll + (k + 1) * per_event_ms - committed)
        next_poll += poll_ms
    mismatched = index_mismatches(store, index)
    return {"events_committed": commits, "consumer_crashes": crashes, "events_replayed": consumer.replayed_events,
            "staleness_p50_ms": round(percentile(stale_ms, 50), 1),
            "staleness_p95_ms": round(percentile(stale_ms, 95), 1),
            "staleness_max_ms": round(max(stale_ms), 1), "events_applied": len(stale_ms),
            "docs_in_db": len(store.docs()), "index_docs_mismatched": mismatched,
            "aborted_transactions": dual_lost, "dual_write_index_updates_lost": dual_lost,
            "embed_calls": index.embed_calls,
            "poll_ms": poll_ms}


# ---------------------------------------------------------------- results
def results() -> dict:
    modes = {m: mode_run(m) for m in MODES}
    crash = {m: crash_loss_trials(m) for m in MODES}
    return {
        "note": NOTE,
        "log_shipping": log_shipping_demo(),
        "modes": modes,
        "crash_trials": crash,
        "read_your_writes": {p: ryw_run(p) for p in ("replica", "primary_window", "lsn_token")},
        "failover": {m: failover_drill(m) for m in MODES} | {"async_no_fencing": failover_drill("async", fencing=False)},
        "bootstrap": bootstrap_compare(),
        "bootstrap_real": bootstrap_real(),
        "leaderless": [leaderless_run(w=w, r=r, read_repair=rr) for (w, r) in ((1, 1), (1, 2), (2, 1), (2, 2), (3, 1))
                       for rr in (False, True)],
        "cdc": cdc_run(),
    }
