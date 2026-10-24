"""Admission control shared across replicas through Redis (fakeredis in-process, real Redis in compose).

Three independent limits, all checked before any model work starts:
1. token bucket per user AND per tenant (request rate, with bursts),
2. daily and monthly LLM-token budgets per tenant (reserve up front, settle with actual usage),
3. max concurrent model calls (a lease set with expiry, so a crashed replica cannot leak slots).
Every rejection carries a Retry-After so callers back off (back pressure).
"""
from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass

BUCKET_LUA = """
local key = KEYS[1]
local rate = tonumber(ARGV[1]); local burst = tonumber(ARGV[2])
local now = tonumber(ARGV[3]); local cost = tonumber(ARGV[4])
local s = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(s[1]); local ts = tonumber(s[2])
if tokens == nil then tokens = burst; ts = now end
if now > ts then tokens = math.min(burst, tokens + (now - ts) * rate); ts = now end
local allowed = 0; local wait = 0
if tokens >= cost then tokens = tokens - cost; allowed = 1 else wait = (cost - tokens) / rate end
redis.call('HSET', key, 'tokens', tostring(tokens), 'ts', tostring(ts))
redis.call('EXPIRE', key, math.ceil(burst / rate) + 60)
return {allowed, tostring(wait)}
"""

BUDGET_LUA = """
local used = tonumber(redis.call('GET', KEYS[1]) or '0')
local amount = tonumber(ARGV[1]); local limit = tonumber(ARGV[2]); local ttl = tonumber(ARGV[3])
if used + amount > limit then return 0 end
redis.call('INCRBY', KEYS[1], amount); redis.call('EXPIRE', KEYS[1], ttl)
return 1
"""

LEASE_LUA = """
local key = KEYS[1]; local limit = tonumber(ARGV[1]); local now = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3]); local id = ARGV[4]
redis.call('ZREMRANGEBYSCORE', key, '-inf', now)
if redis.call('ZCARD', key) >= limit then return 0 end
redis.call('ZADD', key, now + ttl, id)
return 1
"""


@dataclass
class Decision:
    allowed: bool
    reason: str = ""
    retry_after_s: int = 0
    reservation: dict | None = None


@dataclass
class Limits:
    user_rps: float = 5.0
    user_burst: int = 10
    tenant_rps: float = 200.0
    tenant_burst: int = 400
    tenant_daily_tokens: int = 2_000_000
    tenant_monthly_tokens: int = 40_000_000
    max_concurrent_model_calls: int = 64
    lease_ttl_s: float = 30.0


class AdmissionController:
    def __init__(self, redis, limits: Limits | None = None, clock=None, enabled: bool = True) -> None:
        self.r = redis
        self.limits = limits or Limits()
        self.clock = clock
        self.enabled = enabled
        self._bucket = redis.register_script(BUCKET_LUA)
        self._budget = redis.register_script(BUDGET_LUA)
        self._lease = redis.register_script(LEASE_LUA)

    def _now(self) -> float:
        return self.clock.wall() if self.clock else time.time()

    def _take(self, key: str, rate: float, burst: int) -> tuple[bool, float]:
        allowed, wait = self._bucket(keys=[key], args=[rate, burst, self._now(), 1])
        return bool(int(allowed)), float(wait)

    def _periods(self) -> tuple[str, str]:
        t = time.gmtime(self._now())
        return time.strftime("%Y%m%d", t), time.strftime("%Y%m", t)

    def admit(self, tenant: str, user: str, est_tokens: int) -> Decision:
        """Check rate, budget and concurrency. On success the caller MUST call release()."""
        if est_tokens < 0 or not isinstance(est_tokens, int):
            return Decision(False, "invalid_estimate", 0)  # negative estimates would refund budget
        if not self.enabled:
            return Decision(True, reservation={"tenant": tenant, "tokens": 0, "lease": None})
        L = self.limits
        ok, wait = self._take(f"rl:user:{tenant}:{user}", L.user_rps, L.user_burst)
        if not ok:
            return Decision(False, "user_rate", max(1, math.ceil(wait)))
        ok, wait = self._take(f"rl:tenant:{tenant}", L.tenant_rps, L.tenant_burst)
        if not ok:
            return Decision(False, "tenant_rate", max(1, math.ceil(wait)))
        day, month = self._periods()
        dkey, mkey = f"budget:d:{tenant}:{day}", f"budget:m:{tenant}:{month}"
        if not int(self._budget(keys=[dkey], args=[est_tokens, L.tenant_daily_tokens, 2 * 86400])):
            return Decision(False, "daily_token_budget", self._seconds_to_next_day())
        if not int(self._budget(keys=[mkey], args=[est_tokens, L.tenant_monthly_tokens, 32 * 86400])):
            self.r.decrby(dkey, est_tokens)  # undo the daily reservation
            return Decision(False, "monthly_token_budget", 3600)
        lease = uuid.uuid4().hex
        if not int(self._lease(keys=[f"conc:{tenant}"], args=[L.max_concurrent_model_calls, self._now(),
                                                             L.lease_ttl_s, lease])):
            self.r.decrby(dkey, est_tokens)
            self.r.decrby(mkey, est_tokens)
            return Decision(False, "concurrency", 1)
        return Decision(True, reservation={"tenant": tenant, "tokens": est_tokens, "lease": lease,
                                           "dkey": dkey, "mkey": mkey})

    def release(self, reservation: dict, actual_tokens: int) -> None:
        """Free the concurrency slot and settle the budget to actual usage (never below zero delta abuse:
        actual is clamped at >= 0, so a bogus negative 'actual' cannot refund someone else's tokens)."""
        if not reservation or not self.enabled:
            return
        actual = max(0, int(actual_tokens))
        delta = actual - reservation["tokens"]
        if delta:
            self.r.incrby(reservation["dkey"], delta)
            self.r.incrby(reservation["mkey"], delta)
        if reservation.get("lease"):
            self.r.zrem(f"conc:{reservation['tenant']}", reservation["lease"])
        reservation["lease"] = None  # idempotent: a double release does not free a second slot
        reservation["tokens"] = actual

    def _seconds_to_next_day(self) -> int:
        now = self._now()
        return int(86400 - (now % 86400)) or 1

    def usage(self, tenant: str) -> dict:
        day, month = self._periods()
        return {"daily": int(self.r.get(f"budget:d:{tenant}:{day}") or 0),
                "monthly": int(self.r.get(f"budget:m:{tenant}:{month}") or 0),
                "inflight": int(self.r.zcard(f"conc:{tenant}"))}
