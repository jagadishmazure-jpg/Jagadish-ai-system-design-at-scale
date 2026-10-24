"""Talk 16: tail latency and resilience.

Story (fictional): Kestrel, the support assistant for merchants on the made-up Tidewell
storefront platform, got slow for a week with no deploy and no traffic change. The only
clue: the third-party moderation check was slow on about 1 call in 100.

Everything here runs in virtual time with seeded randomness. Nothing calls a real service.

Pieces:
- DEPS / sample_latency: the six calls in one turn and their simulated latency.
- fanout_probability: 1 - (1 - p) ** n, checked against simulation.
- simulate: a FIFO worker-pool (G/G/c) replay of one replica at a given arrival rate.
- replay_incident: healthy vs incident vs fixed, p50/p99 and deadline misses.
- brownout: moderation hangs on every call; workers in flight vs time (Little's law),
  one vs two replicas, and the fixed setup (slow-call breaker + bulkhead + budget timeouts).
- bulkhead_size, hedge_allowed, DeadlineBudget, model_ladder, session/retrieval fallbacks.
"""
from __future__ import annotations

import heapq
import math
import random
import statistics
from dataclasses import dataclass, field

from ..clock import FakeClock
from ..resilience import OPEN, SlowCallBreaker

DEADLINE_S = 20.0
RPM = 400
RPS = RPM / 60.0
WORKERS_PER_REPLICA = 100
BASE_TIMEOUT_S = 30.0
BASE_RETRIES = 3
SLOW_SHARE = 0.01          # incident: share of moderation calls that hit the slow path
SLOW_FLOOR_S = 4.0         # slow path never finishes faster than this, so moderation p99 lands near 4s
SLOW_SIGMA = 1.0
MOD_FALLBACK_S = 0.05      # local strict filter used when the moderation vendor is skipped


@dataclass(frozen=True)
class Dep:
    name: str
    p50_s: float
    p99_s: float
    side_effect: bool = False
    idempotent: bool = True

    @property
    def sigma(self) -> float:
        return math.log(self.p99_s / self.p50_s) / 2.326


DEPS = (
    Dep("session", 0.03, 0.08),
    Dep("order", 0.30, 0.70),
    Dep("retrieval", 0.40, 0.90),
    Dep("moderation_in", 0.15, 0.30),
    Dep("model", 1.75, 3.60, idempotent=False),  # not hedged: it is the expensive call
    Dep("moderation_out", 0.15, 0.30),
)
BY_NAME = {d.name: d for d in DEPS}
MODERATION = ("moderation_in", "moderation_out")
PARALLEL_GROUP = ("order", "retrieval", "moderation_in")  # none needs the others' output


def p50_sum() -> float:
    return round(sum(d.p50_s for d in DEPS), 3)


def sample_latency(dep: Dep, rng: random.Random, incident: bool = False, hang: bool = False) -> float:
    if dep.name in MODERATION:
        if hang:
            return 120.0
        if incident and rng.random() < SLOW_SHARE:
            return SLOW_FLOOR_S * math.exp(abs(rng.gauss(0, SLOW_SIGMA)))
    return dep.p50_s * math.exp(rng.gauss(0, dep.sigma))


def pct(xs, q: float) -> float:
    s = sorted(xs)
    if not s:
        return 0.0
    return s[min(len(s) - 1, int(q * len(s)))]


# ------------------------------------------------------------ fan-out
def fanout_probability(p: float, n: int) -> float:
    """Chance that at least one of n independent calls is slow, each slow with chance p."""
    if not 0 <= p <= 1 or n < 0:
        raise ValueError("p must be in [0, 1] and n >= 0")
    return 1 - (1 - p) ** n


def fanout_simulated(p: float, n: int, trials: int = 20_000, seed: int = 16) -> float:
    rng = random.Random(seed)
    return sum(any(rng.random() < p for _ in range(n)) for _ in range(trials)) / trials


def fanout_curve(p: float = 0.01, ns=(1, 2, 4, 6, 10, 20, 50, 100)) -> list[dict]:
    return [{"n": n, "formula": round(fanout_probability(p, n), 4), "simulated": round(fanout_simulated(p, n), 4)}
            for n in ns]


# ------------------------------------------------------------ sizing helpers
def littles_law_in_flight(rate_per_s: float, seconds_held: float) -> float:
    """L = lambda x W: requests in flight on average."""
    return rate_per_s * seconds_held


def seconds_to_saturate(workers: int, rate_per_s: float) -> float:
    """When every request is stuck, free workers drain at the arrival rate."""
    return workers / rate_per_s


def bulkhead_size(calls_per_turn: float, turns_per_s: float, accepted_latency_s: float, slack: float = 2.0) -> int:
    """Concurrent slots for one dependency: calls/turn x turns/s x accepted latency x slack."""
    if min(calls_per_turn, turns_per_s, accepted_latency_s, slack) <= 0:
        raise ValueError("all inputs must be positive")
    return max(1, math.ceil(calls_per_turn * turns_per_s * accepted_latency_s * slack))


@dataclass
class Bulkhead:
    name: str
    size: int
    in_use: int = 0
    rejected: int = 0

    def try_acquire(self) -> bool:
        if self.in_use >= self.size:
            self.rejected += 1
            return False
        self.in_use += 1
        return True

    def release(self) -> None:
        self.in_use = max(0, self.in_use - 1)


# ------------------------------------------------------------ hedging
def hedge_allowed(dep: Dep | None = None, *, side_effect: bool = False, idempotent: bool = True) -> bool:
    """Only idempotent reads may be duplicated. Writes and side effects never are."""
    if dep is not None:
        side_effect, idempotent = dep.side_effect, dep.idempotent
    return idempotent and not side_effect


def hedge_thresholds(incident: bool = True, n: int = 20_000, seed: int = 7) -> dict:
    """p95 of each dependency, measured from samples (what a real system reads off its histograms)."""
    rng = random.Random(seed)
    return {d.name: pct([sample_latency(d, rng, incident) for _ in range(n)], 0.95) for d in DEPS}


# ------------------------------------------------------------ deadline budget
SLICES = {"session": 0.5, "parallel": 2.5, "model": 12.0, "moderation_out": 1.5, "reserve": 3.5}


class BudgetExceeded(Exception):
    pass


@dataclass
class DeadlineBudget:
    """Fixed slices of one deadline. A step may use its own slice and nothing after it.

    Time a step does not use goes into `bank`, together with the reserve slice. Only
    fallbacks may spend the bank, so a slow step can never eat a later step's slice.
    """

    slices: dict = field(default_factory=lambda: dict(SLICES))
    deadline_s: float = DEADLINE_S

    def __post_init__(self) -> None:
        if abs(sum(self.slices.values()) - self.deadline_s) > 1e-9:
            raise ValueError("slices must add up to the deadline")
        self.bank = self.slices.get("reserve", 0.0)
        self.spent: dict = {}

    def timeout_for(self, step: str, attempts: int = 1) -> float:
        """Per-attempt timeout derived from the slice (replaces a fixed 30s)."""
        return self.slices[step] / max(1, attempts)

    def run(self, step: str, latency_s: float) -> tuple[float, bool]:
        """Charge a primary call. Returns (time used, finished in its slice)."""
        cap = self.slices[step]
        used = min(latency_s, cap)
        self.spent[step] = used
        if latency_s <= cap:
            self.bank += cap - used
            return used, True
        return used, False

    def run_fallback(self, step: str, latency_s: float) -> tuple[float, bool]:
        if latency_s > self.bank:
            used = self.bank
            self.bank = 0.0
            return used, False
        self.bank -= latency_s
        self.spent[step + ":fallback"] = latency_s
        return latency_s, True


# ------------------------------------------------------------ fallback ladder
LADDER = ("primary", "secondary_provider", "small_local_model", "cached_answer", "honest_error")
HONEST_ERROR = ("I can't answer that reliably right now. Please try again in a couple of minutes; "
                "nothing on your account was changed.")
FRESH_SESSION_NOTICE = "I couldn't load our earlier conversation, so I'm starting fresh."
NO_RETRIEVAL_DISCLAIMER = ("I couldn't check the current policy documents, so this answer is from general "
                           "knowledge and may be out of date.")


@dataclass
class LadderResult:
    rung: str
    text: str
    degraded: bool
    tried: list


def model_ladder(available: dict, cached: str | None = None, side_effect: bool = False,
                 latencies: dict | None = None, bank_s: float = float("inf")) -> LadderResult:
    """Walk primary -> secondary -> small local model -> cached answer -> honest error.

    `available` maps rung -> bool. Each rung costs its latency from `bank_s`; a rung that
    does not fit in what is left is skipped. Side effects never degrade: if the primary
    cannot do the write, the answer is the honest error, not a guess from a weaker rung.
    """
    latencies = latencies or {}
    tried = []
    for rung in LADDER[:-1]:
        if side_effect and rung != "primary":
            break
        if rung == "cached_answer":
            ok = cached is not None
        else:
            ok = available.get(rung, False)
        cost = latencies.get(rung, 0.0)
        tried.append(rung)
        if ok and cost <= bank_s:
            text = cached if rung == "cached_answer" else f"answer from {rung}"
            return LadderResult(rung, text, rung != "primary", tried)
    tried.append("honest_error")
    return LadderResult("honest_error", HONEST_ERROR, True, tried)


def load_session(store_ok: bool, history: list | None = None) -> dict:
    if store_ok:
        return {"history": list(history or []), "notice": None, "degraded": False}
    return {"history": [], "notice": FRESH_SESSION_NOTICE, "degraded": True}


def answer_with_retrieval(retrieval_ok: bool, docs: list | None = None) -> dict:
    if retrieval_ok:
        return {"grounded": True, "docs": list(docs or []), "disclaimer": None}
    return {"grounded": False, "docs": [], "disclaimer": NO_RETRIEVAL_DISCLAIMER}


# ------------------------------------------------------------ one call, one turn
def call_dep(dep: Dep, rng: random.Random, *, incident: bool, timeout_s: float, retries: int,
             hedge_at: float | None = None, hang: bool = False, stats: dict | None = None) -> tuple[float, bool]:
    elapsed = 0.0
    for _ in range(retries + 1):
        lat = sample_latency(dep, rng, incident, hang)
        if stats is not None:
            stats["calls"] = stats.get("calls", 0) + 1
        if hedge_at is not None and hedge_allowed(dep) and lat > hedge_at:
            if stats is not None:
                stats["hedges"] = stats.get("hedges", 0) + 1
                stats["calls"] += 1
            lat = min(lat, hedge_at + sample_latency(dep, rng, incident, hang))
        if lat <= timeout_s:
            return elapsed + lat, True
        elapsed += timeout_s
    return elapsed, False


@dataclass
class Policy:
    name: str = "baseline"
    parallel: bool = False
    hedge: bool = False
    budget: bool = False       # timeouts from DeadlineBudget slices, fallbacks from the bank
    timeout_s: float = BASE_TIMEOUT_S
    retries: int = BASE_RETRIES


BASELINE = Policy()
PARALLEL_ONLY = Policy("parallel only", parallel=True)
FIXED = Policy("fixed", parallel=True, hedge=True, budget=True, retries=1)


def turn_service(rng: random.Random, policy: Policy, incident: bool, hedges: dict | None,
                 stats: dict) -> tuple[float, bool, bool]:
    """Return (seconds, ok, degraded) for the work of one turn (no queueing)."""
    kw = {"incident": incident, "stats": stats}

    def call(name, timeout, retries):
        h = hedges.get(name) if hedges else None
        return call_dep(BY_NAME[name], rng, timeout_s=timeout, retries=retries, hedge_at=h, **kw)

    if not policy.budget:
        t_ = {n: call(n, policy.timeout_s, policy.retries) for n in BY_NAME}
        ok = all(v[1] for v in t_.values())
        if policy.parallel:
            secs = (t_["session"][0] + max(t_[n][0] for n in PARALLEL_GROUP) + t_["model"][0]
                    + t_["moderation_out"][0])
        else:
            secs = sum(v[0] for v in t_.values())
        return secs, ok, False

    b = DeadlineBudget()
    degraded = False
    total = 0.0
    att = policy.retries + 1
    s, ok = call("session", b.timeout_for("session", att), policy.retries)
    used, fits = b.run("session", s if ok else math.inf)
    total += used
    degraded |= not (ok and fits)  # fresh session + notice; no extra time
    group = [call(n, b.timeout_for("parallel", att), policy.retries) for n in PARALLEL_GROUP]
    g = max(x[0] for x in group) if all(x[1] for x in group) else math.inf
    used, fits = b.run("parallel", g)
    total += used
    if not fits:
        # moderation -> local strict filter, retrieval -> model-knowledge answer with a disclaimer
        fb, ok_fb = b.run_fallback("parallel", MOD_FALLBACK_S)
        total += fb
        degraded = True
        if not ok_fb:
            return total, False, True
    m, ok = call("model", b.timeout_for("model", 1), 0)
    used, fits = b.run("model", m if ok else math.inf)
    total += used
    if not (ok and fits):
        fb, ok_fb = b.run_fallback("model", 1.2)  # secondary provider / small local model
        total += fb
        if not ok_fb:
            return total, False, True
        degraded = True
    o, ok = call("moderation_out", b.timeout_for("moderation_out", att), policy.retries)
    used, fits = b.run("moderation_out", o if ok else math.inf)
    total += used
    if not (ok and fits):
        fb, ok_fb = b.run_fallback("moderation_out", MOD_FALLBACK_S)
        total += fb
        degraded = True
        if not ok_fb:
            return total, False, True
    return total, True, degraded


def simulate(policy: Policy = BASELINE, *, incident: bool, turns: int = 20_000, rps: float = RPS,
             workers: int = WORKERS_PER_REPLICA, seed: int = 16) -> dict:
    rng = random.Random(seed)
    arr_rng = random.Random(seed + 1)
    hedges = hedge_thresholds(incident) if policy.hedge else None
    free = [0.0] * workers
    heapq.heapify(free)
    t = 0.0
    lat, misses, failed, degraded, waits = [], 0, 0, 0, []
    stats: dict = {}
    for _ in range(turns):
        t += arr_rng.expovariate(rps)
        start = max(t, heapq.heappop(free))
        svc, ok, deg = turn_service(rng, policy, incident, hedges, stats)
        heapq.heappush(free, start + svc)
        total = start - t + svc
        waits.append(start - t)
        lat.append(total)
        failed += not ok
        degraded += deg
        misses += (not ok) or total > DEADLINE_S
    base_calls = turns * len(DEPS)
    return {"policy": policy.name, "incident": incident, "turns": turns,
            "p50_s": round(statistics.median(lat), 3), "p95_s": round(pct(lat, 0.95), 3),
            "p99_s": round(pct(lat, 0.99), 3), "deadline_miss_rate": round(misses / turns, 4),
            "failed_rate": round(failed / turns, 4), "degraded_rate": round(degraded / turns, 4),
            "max_wait_s": round(max(waits), 3),
            "hedge_extra_load": round(stats.get("hedges", 0) / base_calls, 4),
            "hedges": stats.get("hedges", 0)}


def moderation_p99(incident: bool, n: int = 50_000, seed: int = 3) -> float:
    rng = random.Random(seed)
    return round(pct([sample_latency(BY_NAME["moderation_in"], rng, incident) for _ in range(n)], 0.99), 3)


def replay_incident(turns: int = 20_000) -> dict:
    return {
        "p50_sum_s": p50_sum(),
        "moderation_p99_s": {"healthy": moderation_p99(False), "incident": moderation_p99(True)},
        "healthy": simulate(BASELINE, incident=False, turns=turns),
        "incident": simulate(BASELINE, incident=True, turns=turns),
        "incident_parallel_only": simulate(PARALLEL_ONLY, incident=True, turns=turns),
        "incident_fixed": simulate(FIXED, incident=True, turns=turns),
        "turn_slow_share": {"calls_per_turn_moderation": 2,
                            "formula": round(fanout_probability(SLOW_SHARE, 2), 4)},
    }


def parallel_vs_sequential(n: int = 20_000, seed: int = 5) -> dict:
    """Same samples, laid out sequentially vs with the independent calls in parallel."""
    rng = random.Random(seed)
    seq, par = [], []
    for _ in range(n):
        x = {d.name: sample_latency(d, rng, incident=True) for d in DEPS}
        seq.append(sum(x.values()))
        par.append(x["session"] + max(x[k] for k in PARALLEL_GROUP) + x["model"] + x["moderation_out"])
    r = {"seq_p50": statistics.median(seq), "par_p50": statistics.median(par),
         "seq_p99": pct(seq, 0.99), "par_p99": pct(par, 0.99), "seq_p999": pct(seq, 0.999),
         "par_p999": pct(par, 0.999)}
    return {k: round(v, 3) for k, v in r.items()}


# ------------------------------------------------------------ brownout: workers stuck waiting
def brownout(replicas: int = 1, fixed: bool = False, seconds: float = 120.0, start_s: float = 10.0,
             rps: float = RPS, seed: int = 21, use_breaker: bool = True) -> dict:
    """Moderation hangs on every call from `start_s`. Track busy workers each second.

    Baseline: 30s timeout x (1 + 3 retries) per moderation call, no breaker, no bulkhead.
    Fixed: budget-derived timeouts, a SlowCallBreaker on moderation, and a moderation
    bulkhead sized with bulkhead_size(); skipped moderation uses the local strict filter.
    Requests are spread evenly across replicas, each with its own worker pool.
    """
    rng = random.Random(seed)
    workers = WORKERS_PER_REPLICA * replicas
    clock = FakeClock(0.0)
    breaker = SlowCallBreaker(clock=clock, slow_call_s=1.0, cooldown_s=10.0)
    bh = Bulkhead("moderation", bulkhead_size(2, rps, BY_NAME["moderation_in"].p99_s, slack=3.0))
    mod_inflight: list[float] = []
    pending: list[tuple[float, float, bool]] = []  # (end time, latency, ok) -> breaker
    busy: list[float] = []
    queue_wait = []
    timeline = []
    t, misses, total = 0.0, 0, 0
    budget = DeadlineBudget()
    stuck_at = None
    arr = random.Random(seed + 1)
    events = []
    while t < seconds:
        t += arr.expovariate(rps)
        events.append(t)
    free = [0.0] * workers
    heapq.heapify(free)
    for t in events:
        start = max(t, heapq.heappop(free))
        hang = start >= start_s
        svc = 0.0
        ok = True
        for name in ("session", "order", "retrieval", "moderation_in", "model", "moderation_out"):
            dep = BY_NAME[name]
            if name in MODERATION and fixed:
                now = start + svc
                clock.t = now
                while use_breaker and pending and pending[0][0] <= now:
                    _, lt, okk = heapq.heappop(pending)
                    breaker.record_call(lt, okk)
                while mod_inflight and mod_inflight[0] <= now:
                    heapq.heappop(mod_inflight)
                    bh.release()
                if (not use_breaker or breaker.allow()) and bh.try_acquire():
                    to = budget.timeout_for("parallel" if name == "moderation_in" else name, 2)
                    lat, okk = call_dep(dep, rng, incident=False, timeout_s=to, retries=1, hang=hang)
                    heapq.heappush(mod_inflight, now + lat)
                    heapq.heappush(pending, (now + lat, lat, okk))
                    svc += lat if okk else lat + MOD_FALLBACK_S
                else:
                    svc += MOD_FALLBACK_S
                continue
            lat, okk = call_dep(dep, rng, incident=False, timeout_s=BASE_TIMEOUT_S,
                                retries=BASE_RETRIES, hang=hang)
            svc += lat
            ok &= okk
        heapq.heappush(free, start + svc)
        busy.append((start, start + svc))
        total += 1
        e2e = start - t + svc
        queue_wait.append(start - t)
        misses += (not ok) or e2e > DEADLINE_S
        if stuck_at is None and start - t > 1.0:
            stuck_at = round(t, 1)
    for sec in range(int(seconds)):
        timeline.append(sum(1 for a, b in busy if a <= sec < b))
    held = BASE_TIMEOUT_S * (BASE_RETRIES + 1) if not fixed else None
    return {"replicas": replicas, "fixed": fixed, "workers": workers, "brownout_starts_s": start_s,
            "saturated_at_s": stuck_at,
            "seconds_after_brownout": None if stuck_at is None else round(stuck_at - start_s, 1),
            "peak_busy_workers": max(timeline), "busy_timeline": timeline,
            "deadline_miss_rate": round(misses / total, 4), "turns": total,
            "littles_law_in_flight_30s": round(littles_law_in_flight(rps, BASE_TIMEOUT_S), 1),
            "littles_law_in_flight_with_retries": None if held is None else round(littles_law_in_flight(rps, held), 1),
            "breaker_slow_trips": breaker.slow_trips if fixed and use_breaker else None,
            "breaker_failure_trips": breaker.failure_trips if fixed and use_breaker else None, "use_breaker": use_breaker,
            "breaker_open_at_end": (breaker.state == OPEN) if fixed and use_breaker else None,
            "moderation_bulkhead": bh.size if fixed else None, "bulkhead_rejected": bh.rejected if fixed else None}


def results(turns: int = 20_000) -> dict:
    """Everything the doc and the evidence board quote, from one deterministic run."""
    bo = {k: brownout(**kw) for k, kw in {
        "one_replica": {}, "two_replicas": {"replicas": 2}, "fixed": {"fixed": True},
        "bulkhead_only": {"fixed": True, "use_breaker": False}}.items()}
    return {"note": "Simulated, virtual time, seeded. Not a measurement of any real service.",
            "incident": replay_incident(turns), "parallel_vs_sequential": parallel_vs_sequential(),
            "fanout_curve_p1pct": fanout_curve(), "brownout": bo,
            "bulkhead_moderation": bulkhead_size(2, RPS, BY_NAME["moderation_in"].p99_s, slack=3.0),
            "budget_slices_s": dict(SLICES)}
