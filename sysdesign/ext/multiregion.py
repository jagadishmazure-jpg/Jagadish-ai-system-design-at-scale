"""Two-region multi-primary writes, conflict handling, clock skew and the hybrid commit mode.

Simulated with fixed seeds. The AI angle: an agent's long-term memory (a set of saved facts) and a
user's preferences (reply language, tone, units) are edited in two regions at about the same time,
for example a user on a phone in one region while an agent in the other region saves a fact.

Causality is tracked exactly with version vectors, so for every write the run knows which earlier
writes it had seen. That lets it count, per strategy:
* last-writer-wins (LWW) on the whole record by wall-clock timestamp: updates silently dropped, split
  into true concurrent edits and edits dropped only because a region's clock was behind;
* LWW with a hybrid logical clock (HLC): a write is always stamped later than anything it has seen,
  so clock skew can no longer reorder cause and effect (concurrent edits are still dropped);
* version vectors that detect concurrent writes and keep both versions (siblings) for resolution;
* a custom merge: memory facts are unioned, preferences merge per field, and only a true
  same-field conflict with different values is flagged for a person.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from sysdesign.observability import percentile

NOTE = "simulated (fixed seeds); times are simulated ms, counts are exact counts from the runs"
REGIONS = ("east", "west")
PREF_FIELDS = ("language", "tone", "units")


# ---------------------------------------------------------------- clocks
def vv_leq(a: dict, b: dict) -> bool:
    return all(a.get(k, 0) <= b.get(k, 0) for k in set(a) | set(b))


def vv_concurrent(a: dict, b: dict) -> bool:
    return not vv_leq(a, b) and not vv_leq(b, a)


def vv_merge(a: dict, b: dict) -> dict:
    return {k: max(a.get(k, 0), b.get(k, 0)) for k in set(a) | set(b)}


@dataclass
class HybridClock:
    """Hybrid logical clock: (wall ms, counter). Never goes backwards, and moves past any stamp seen."""
    wall: float = 0.0
    counter: int = 0

    def now(self, physical_ms: float) -> tuple[float, int]:
        if physical_ms > self.wall:
            self.wall, self.counter = physical_ms, 0
        else:
            self.counter += 1
        return (self.wall, self.counter)

    def observe(self, stamp: tuple[float, int], physical_ms: float) -> None:
        w = max(self.wall, stamp[0], physical_ms)
        if w == self.wall and w == stamp[0]:
            self.counter = max(self.counter, stamp[1]) + 1
        elif w == stamp[0]:
            self.counter = stamp[1] + 1
        elif w == self.wall:
            self.counter += 1
        else:
            self.counter = 0
        self.wall = w


# ---------------------------------------------------------------- the workload
@dataclass
class Write:
    wid: int
    region: str
    t: float                         # true time
    kind: str                        # "fact" or "pref"
    fld: str                         # fact text or preference field
    value: str
    vv: dict = field(default_factory=dict)
    wall_ts: tuple = ()              # (skewed wall clock, region) for plain LWW
    hlc_ts: tuple = ()               # (hlc wall, counter, region)


def make_record_writes(rng: random.Random, n_writes: int, span_ms: float, delay_ms: float, skew_ms: float,
                       fact_share: float = 0.5) -> list[Write]:
    """Writes to ONE record (one user's memory + preferences), with exact causality.

    A region knows its own earlier writes immediately and the other region's writes delay_ms later.
    The west region's clock runs skew_ms behind true time.
    """
    raw = sorted((rng.uniform(0, span_ms), rng.choice(REGIONS)) for _ in range(n_writes))
    hlc = {r: HybridClock() for r in REGIONS}
    seen_hlc: dict[str, set[int]] = {r: set() for r in REGIONS}
    out: list[Write] = []
    for i, (t, region) in enumerate(raw):
        known = [w for w in out if w.region == region or w.t + delay_ms <= t]
        vv: dict = {}
        for w in known:
            vv = vv_merge(vv, w.vv)
        vv[region] = vv.get(region, 0) + 1
        physical = t - (skew_ms if region == "west" else 0.0)
        for w in known:                   # the HLC hears every stamp that replication delivered
            if w.wid not in seen_hlc[region]:
                hlc[region].observe(w.hlc_ts[:2], physical)
                seen_hlc[region].add(w.wid)
        h = hlc[region].now(physical)
        if rng.random() < fact_share:
            kind, fld, value = "fact", f"fact-{i}", "saved"
        else:
            kind, fld, value = "pref", rng.choice(PREF_FIELDS), rng.choice(("a", "b", "c"))
        w = Write(i, region, t, kind, fld, value, vv, (physical, region), (h[0], h[1], region))
        seen_hlc[region].add(i)
        out.append(w)
    return out


def causal_past(w: Write, writes: list[Write]) -> set[int]:
    return {x.wid for x in writes if x.wid != w.wid and vv_leq(x.vv, w.vv)}


def state_at(w: Write, writes: list[Write]) -> dict:
    """The record as the writing region saw it right after write w (its causal past plus w)."""
    past = sorted((x for x in writes if x.wid in causal_past(w, writes) or x.wid == w.wid),
                  key=lambda x: (sum(x.vv.values()), x.t))
    facts, prefs = set(), {}
    for x in past:
        if x.kind == "fact":
            facts.add(x.fld)
        else:
            prefs[x.fld] = x
    return {"facts": facts, "prefs": prefs}


def survived(x: Write, final: dict, writes: list[Write]) -> bool:
    """Did write x's effect reach the final state, or was it legitimately overwritten by a later write?"""
    if x.kind == "fact":
        return x.fld in final["facts"]
    cur = final["prefs"].get(x.fld)
    if cur is not None and cur.wid == x.wid:
        return True
    # a later write to the same field that had already seen x replaced it on purpose
    return any(y.kind == "pref" and y.fld == x.fld and y.wid != x.wid and vv_leq(x.vv, y.vv) for y in writes)


def lww(writes: list[Write], clock: str) -> dict:
    key = (lambda w: w.wall_ts) if clock == "wall" else (lambda w: w.hlc_ts)
    winner = max(writes, key=key)
    final = state_at(winner, writes)
    dropped = [x for x in writes if not survived(x, final, writes)]
    wrong_order = [x for x in dropped if winner.wid in causal_past(x, writes)]
    return {"dropped": len(dropped), "dropped_concurrent": len(dropped) - len(wrong_order),
            "dropped_wrong_order": len(wrong_order)}


def maximal(writes: list[Write]) -> list[Write]:
    return [w for w in writes if not any(o.wid != w.wid and vv_leq(w.vv, o.vv) for o in writes)]


def version_vectors(writes: list[Write]) -> dict:
    """Keep every write no other write has seen (siblings). Nothing is dropped silently."""
    sib = maximal(writes)
    return {"siblings": len(sib), "conflict_flagged": len(sib) > 1, "dropped": 0}


def custom_merge(writes: list[Write]) -> dict:
    """Union the facts; per preference field keep the causally latest write; flag only real clashes."""
    facts = {w.fld for w in writes if w.kind == "fact"}
    flagged = 0
    prefs = {}
    for f in PREF_FIELDS:
        fw = [w for w in writes if w.kind == "pref" and w.fld == f]
        if not fw:
            continue
        top = maximal(fw)
        if len({w.value for w in top}) > 1:
            flagged += 1                      # a person (or the user) picks; nothing is thrown away
        prefs[f] = max(top, key=lambda w: w.hlc_ts)
    final = {"facts": facts, "prefs": prefs}
    dropped = [x for x in writes if not survived(x, final, writes)
               and not (x.kind == "pref" and any(x.wid == w.wid for w in maximal(
                   [w for w in writes if w.kind == "pref" and w.fld == x.fld])))]
    return {"dropped": len(dropped), "fields_flagged": flagged}


def conflict_run(records: int = 2000, writes_per_record: int = 6, span_ms: float = 2000.0,
                 delay_ms: float = 120.0, skew_ms: float = 0.0, seed: int = 51) -> dict:
    rng = random.Random(seed)
    tot = {"writes": 0, "lww_wall_dropped": 0, "lww_wall_concurrent": 0, "lww_wall_wrong_order": 0,
           "lww_hlc_dropped": 0, "lww_hlc_wrong_order": 0, "vv_records_flagged": 0, "vv_dropped": 0,
           "merge_dropped": 0, "merge_fields_flagged": 0, "records_with_concurrency": 0}
    for _ in range(records):
        ws = make_record_writes(rng, writes_per_record, span_ms, delay_ms, skew_ms)
        tot["writes"] += len(ws)
        a, b = lww(ws, "wall"), lww(ws, "hlc")
        v, m = version_vectors(ws), custom_merge(ws)
        tot["lww_wall_dropped"] += a["dropped"]
        tot["lww_wall_concurrent"] += a["dropped_concurrent"]
        tot["lww_wall_wrong_order"] += a["dropped_wrong_order"]
        tot["lww_hlc_dropped"] += b["dropped"]
        tot["lww_hlc_wrong_order"] += b["dropped_wrong_order"]
        tot["vv_records_flagged"] += v["conflict_flagged"]
        tot["vv_dropped"] += v["dropped"]
        tot["merge_dropped"] += m["dropped"]
        tot["merge_fields_flagged"] += m["fields_flagged"]
        tot["records_with_concurrency"] += any(vv_concurrent(x.vv, y.vv) for x in ws for y in ws if x.wid < y.wid)
    tot.update(records=records, delay_ms=delay_ms, skew_ms=skew_ms,
               lww_wall_drop_rate=round(tot["lww_wall_dropped"] / tot["writes"], 4),
               lww_hlc_drop_rate=round(tot["lww_hlc_dropped"] / tot["writes"], 4))
    return tot


def skew_sweep(skews=(0, 5, 50, 200, 500, 2000), records: int = 1000, seed: int = 52) -> list[dict]:
    return [conflict_run(records=records, skew_ms=s, seed=seed) for s in skews]


# ---------------------------------------------------------------- hybrid commit mode
HYBRID_MODES = ("all-async", "local-sync+remote-async", "all-sync")


def hybrid_run(mode: str, writes: int = 5000, rate_per_s: float = 500.0, local_ms: float = 0.4,
               in_region_ms: float = 1.2, cross_region_ms: float = 70.0, seed: int = 61) -> dict:
    """Write latency vs data at risk. Each region: a primary plus one in-region replica; one remote region.

    Node failure: the primary node dies; at risk = acked writes not yet on the in-region replica.
    Region failure: the whole region is lost; at risk = acked writes not yet in the remote region.
    """
    if mode not in HYBRID_MODES:
        raise ValueError(mode)
    rng = random.Random(seed)
    t, lat, rows = 0.0, [], []
    for _ in range(writes):
        t += rng.expovariate(rate_per_s / 1000.0)
        local_copy = in_region_ms * (0.5 + rng.expovariate(2.0))
        remote_copy = cross_region_ms * (0.9 + rng.expovariate(10.0))
        wait = {"all-async": 0.0, "local-sync+remote-async": local_copy,
                "all-sync": max(local_copy, remote_copy)}[mode]
        lat.append(local_ms + wait)
        rows.append((t + local_ms + wait, t + local_ms + local_copy, t + local_ms + remote_copy))
    crashes = [rng.uniform(rows[100][0], rows[-1][0]) for _ in range(300)]
    node_risk, region_risk = [], []
    for c in crashes:
        acked = [r for r in rows if r[0] <= c]
        node_risk.append(sum(1 for r in acked if r[1] > c))
        region_risk.append(sum(1 for r in acked if r[2] > c))
    return {"mode": mode, "writes": writes, "write_p50_ms": round(percentile(lat, 50), 2),
            "write_p95_ms": round(percentile(lat, 95), 2), "crash_points": len(crashes),
            "node_failure_acked_lost_mean": round(sum(node_risk) / len(crashes), 3),
            "region_failure_acked_lost_mean": round(sum(region_risk) / len(crashes), 3),
            "region_failure_acked_lost_p95": percentile(region_risk, 95)}


def results() -> dict:
    return {"note": NOTE, "conflicts": conflict_run(), "skew_sweep": skew_sweep(),
            "hybrid": {m: hybrid_run(m) for m in HYBRID_MODES}}
