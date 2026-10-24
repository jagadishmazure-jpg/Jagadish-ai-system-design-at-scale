"""Talk 17 add-on: retries that cannot pile up.

- RetryBudget: retries may use at most `ratio` of recent request volume (10% by default, so 40 a
  minute at 400 requests a minute). When the budget is empty a failing call fails fast with 503
  instead of retrying.
- call_with_budget: the existing RetryPolicy (max_retries) plus the budget.
- in-flight duplicates: a second click with the same idempotency key, arriving while the first is
  still running, waits on the job ledger and gets the one stored result; the side effect runs once.
- amplification: calls that reach the bottom service per user click, retries in one layer vs in
  every layer, simulated with a seeded failure rate.
"""
from __future__ import annotations

import asyncio
import json
import random
from collections import deque

from ..clock import RealClock
from ..resilience import RetryPolicy

QUOTED_AMPLIFICATION_WITH_BUDGET = {"value": 1.1, "source": "quoted from the talk; not measured here"}


class RetryBudget:
    def __init__(self, ratio: float = 0.10, window_s: float = 60.0, min_per_window: int = 0, clock=None):
        if ratio < 0 or window_s <= 0 or min_per_window < 0:
            raise ValueError("ratio, window and minimum must not be negative")
        self.ratio, self.window_s, self.min = ratio, window_s, min_per_window
        self.clock = clock or RealClock()
        self._requests: deque = deque()
        self._retries: deque = deque()
        self.denied = 0

    def _trim(self) -> None:
        cut = self.clock.now() - self.window_s
        for q in (self._requests, self._retries):
            while q and q[0] < cut:
                q.popleft()

    def record_request(self) -> None:
        self._requests.append(self.clock.now())

    def allowance(self) -> int:
        self._trim()
        return max(self.min, int(len(self._requests) * self.ratio))

    def try_spend(self) -> bool:
        self._trim()
        if len(self._retries) < self.allowance():
            self._retries.append(self.clock.now())
            return True
        self.denied += 1
        return False


class Unavailable(Exception):
    """Fail fast: maps to HTTP 503 with retry-after."""
    status = 503


def call_with_budget(fn, policy: RetryPolicy, budget: RetryBudget | None):
    """Try once, then retry up to policy.max_retries while the budget allows."""
    if budget is not None:
        budget.record_request()
    attempts = 0
    while True:
        attempts += 1
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - any downstream failure counts
            if attempts > policy.max_retries:
                raise Unavailable(f"gave up after {attempts} attempts") from e
            if budget is not None and not budget.try_spend():
                raise Unavailable("retry budget empty") from e


def http_status(fn, policy: RetryPolicy, budget: RetryBudget | None) -> int:
    try:
        call_with_budget(fn, policy, budget)
        return 200
    except Unavailable:
        return 503


# ------------------------------------------------------------------ amplification
def amplification(fail_rate: float, layers: int = 3, retry_layers: str = "one", clicks: int = 4000,
                  policy: RetryPolicy | None = None, budget_ratio: float | None = None, seed: int = 17) -> dict:
    """Each click goes client -> gateway -> service -> store. retry_layers='one' retries only at the
    gateway; 'all' retries in every layer. Returns calls that reach the store per click."""
    policy = policy or RetryPolicy()
    rng = random.Random(seed)
    calls = {"store": 0}

    class Clock:
        t = 0.0

        def now(self):
            return self.t

    clock = Clock()
    budgets = [RetryBudget(budget_ratio, clock=clock) if budget_ratio is not None else None
               for _ in range(layers)]

    def store():
        calls["store"] += 1
        if rng.random() < fail_rate:
            raise RuntimeError("store failed")
        return "ok"

    def layer(i):
        inner = store if i == layers - 1 else (lambda: layer(i + 1))
        retrying = retry_layers == "all" or (retry_layers == "one" and i == 0)
        if not retrying:
            if budgets[i] is not None:
                budgets[i].record_request()
            return inner()  # a failure passes straight up
        return call_with_budget(inner, policy, budgets[i])

    ok = 0
    for n in range(clicks):
        clock.t = n * 0.15  # 400 clicks a minute
        try:
            layer(0)
            ok += 1
        except Exception:  # noqa: BLE001 - the click failed
            pass
    return {"fail_rate": fail_rate, "retry_layers": retry_layers, "budget_ratio": budget_ratio,
            "calls_per_click": round(calls["store"] / clicks, 3), "success_rate": round(ok / clicks, 4),
            "worst_case_calls": (policy.max_retries + 1) ** (layers if retry_layers == "all" else 1)}


# ------------------------------------------------------------------ in-flight duplicates
async def click(queue, shop, user_id: str, order_id: str, amount: float, key: str, work_s: float = 0.05,
                poll_s: float = 0.005, pool: str = "interactive") -> dict:
    """One button press. Uses the job ledger (JobQueue idempotency) so a duplicate press while the
    first is running waits for the stored result instead of running the refund again."""
    jid = queue.enqueue(pool, {"kind": "refund", "user_id": user_id, "order_id": order_id, "amount": amount},
                        idempotency_key=key)
    job = queue.claim(pool)
    if job is not None and job.job_id == jid:
        await asyncio.sleep(work_s)  # the slow part: the refund call is in flight
        rec = shop.issue_refund(order_id, user_id, amount, idempotency_key=key)
        queue.r.hset(f"{queue.p}:job:{jid}", "result", json.dumps(rec))
        queue.ack(job)
        return {"job_id": jid, "ran": True, "result": rec}
    if job is not None:  # claimed someone else's job: put it back
        queue.nack(job, "not mine")
    waited = 0
    while queue.status(jid) != "done":
        await asyncio.sleep(poll_s)
        waited += 1
    rec = json.loads(queue.r.hget(f"{queue.p}:job:{jid}", "result"))
    return {"job_id": jid, "ran": False, "result": rec, "polls": waited}


async def double_click(queue, shop, gap_s: float = 0.01, work_s: float = 0.05) -> dict:
    a = asyncio.create_task(click(queue, shop, "u-ava", "KG-100002", 10.0, "dbl-click-0001", work_s=work_s))
    await asyncio.sleep(gap_s)
    b = asyncio.create_task(click(queue, shop, "u-ava", "KG-100002", 10.0, "dbl-click-0001", work_s=work_s))
    ra, rb = await a, await b
    return {"first": ra, "second": rb, "refunds_issued": len(shop.refunds),
            "issue_refund_calls": shop.calls.count("issue_refund")}


def results() -> dict:
    out = {"note": "simulated: seeded failures, virtual clock", "budget_at_400_rpm": None, "amplification": []}

    class C:
        t = 0.0

        def now(self):
            return self.t

    c = C()
    b = RetryBudget(0.10, clock=c)
    for i in range(400):
        c.t = i * 0.15
        b.record_request()
    out["budget_at_400_rpm"] = b.allowance()
    for f in (0.05, 0.2, 1.0):
        for mode in ("one", "all"):
            out["amplification"].append(amplification(f, retry_layers=mode))
            out["amplification"].append(amplification(f, retry_layers=mode, budget_ratio=0.10))
    out["quoted"] = QUOTED_AMPLIFICATION_WITH_BUDGET
    return out
