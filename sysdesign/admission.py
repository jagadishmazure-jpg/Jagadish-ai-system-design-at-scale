"""Admission control shared across replicas through Redis.

Three gates, checked in order, each returning a Retry-After hint:
1. token bucket per (tenant, user) on request rate,
2. daily and monthly LLM-token budgets per (tenant, user),
3. a cluster-wide cap on concurrent model calls (leased, so a crashed
   replica's slots expire instead of leaking).
All state lives in Redis and every check is a single Lua script, so
replicas cannot race each other into over-admitting.
"""
from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass

BUCKET_LUA = """
local key = KEYS[1]
local rate = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])
local s = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(s[1]) or burst
local ts = tonumber(s[2]) or now
if now > ts then tokens = math.min(burst, tokens + (now - ts) * rate) end
local allowed = 0
local retry = 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
else
  retry = (cost - tokens) / rate
end
redis.call('HSET', key, 'tokens', tostring(tokens), 'ts', tostring(math.max(now, ts)))
redis.call('EXPIRE', key, math.ceil(burst / rate) + 60)
return {allowed, tostring(retry)}
"""

BUDGET_LUA = """
-- KEYS: day, month ; ARGV: amount, day_limit, month_limit, day_ttl, month_ttl
local amt = tonumber(ARGV[1])
local d = tonumber(redis.call('GET', KEYS[1]) or '0')
local m = tonumber(redis.call('GET', KEYS[2]) or '0')
if d + amt > tonumber(ARGV[2]) then return {0, 'day'} end
if m + amt > tonumber(ARGV[3]) then return {0, 'month'} end
redis.call('INCRBY', KEYS[1], amt)
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[4]))
redis.call('INCRBY', KEYS[2], amt)
redis.call('EXPIRE', KEYS[2], tonumber(ARGV[5]))
return {1, 'ok'}
"""

SEM_LUA = """
-- KEYS: zset ; ARGV: now, limit, lease, holder
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
if redis.call('ZCARD', KEYS[1]) < tonumber(ARGV[2]) then
  redis.call('ZADD', KEYS[1], tonumber(ARGV[1]) + tonumber(ARGV[3]), ARGV[4])
  return 1
end
return 0
"""


@dataclass(frozen=True)
class Limits:
    rate_per_s: float = 2.0
    burst: int = 10
    tokens_per_day: int = 50_000
    tokens_per_month: int = 600_000
    max_concurrent_model_calls: int = 32
    lease_s: float = 30.0


TIER_LIMITS = {
    "standard": Limits(),
    "premium": Limits(rate_per_s=5.0, burst=20, tokens_per_day=200_000, tokens_per_month=3_000_000),
}


@dataclass
class Decision:
    allowed: bool
    reason: str = "ok"
    retry_after_s: int = 0


class Admission:
    def __init__(self, redis, clock=None, limits: dict | None = None, prefix: str = "adm") -> None:
        self.r = redis
        self.clock = clock
        self.limits = limits or TIER_LIMITS
        self.p = prefix
        self._bucket = redis.register_script(BUCKET_LUA)
        self._budget = redis.register_script(BUDGET_LUA)
        self._sem = redis.register_script(SEM_LUA)

    def _now(self) -> float:
        return self.clock.now() if self.clock else time.time()

    def limits_for(self, tier: str) -> Limits:
        return self.limits.get(tier, self.limits["standard"])  # unknown tier never gets more

    def check_rate(self, tenant: str, user: str, tier: str) -> Decision:
        lim = self.limits_for(tier)
        allowed, retry = self._bucket(keys=[f"{self.p}:tb:{tenant}:{user}"],
                                      args=[lim.rate_per_s, lim.burst, self._now(), 1])
        if int(allowed):
            return Decision(True)
        return Decision(False, "rate", max(1, math.ceil(float(retry))))

    def _budget_keys(self, tenant: str, user: str) -> tuple[str, str]:
        t = time.gmtime(self._now())
        return (f"{self.p}:bd:{tenant}:{user}:{t.tm_year}{t.tm_yday:03d}",
                f"{self.p}:bm:{tenant}:{user}:{t.tm_year}{t.tm_mon:02d}")

    def reserve_tokens(self, tenant: str, user: str, tier: str, estimate: int) -> Decision:
        if not isinstance(estimate, int) or estimate < 0:
            raise ValueError("token estimate must be a non-negative int")  # negative would refill the budget
        lim = self.limits_for(tier)
        day, month = self._budget_keys(tenant, user)
        ok, which = self._budget(keys=[day, month],
                                 args=[estimate, lim.tokens_per_day, lim.tokens_per_month, 2 * 86400, 32 * 86400])
        if int(ok):
            return Decision(True)
        which = which.decode() if isinstance(which, bytes) else which
        now = self._now()
        t = time.gmtime(now)
        if which == "day":
            retry = 86400 - (t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec)
        else:  # seconds until the next UTC month starts
            import calendar

            days_left = calendar.monthrange(t.tm_year, t.tm_mon)[1] - t.tm_mday
            retry = days_left * 86400 + 86400 - (t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec)
        return Decision(False, f"budget_{which}", int(retry))

    def settle_tokens(self, tenant: str, user: str, reserved: int, actual: int) -> None:
        """Charge the difference between the reservation and actual usage (can be negative = refund)."""
        if reserved < 0 or actual < 0:
            raise ValueError("token counts must be non-negative")
        delta = actual - reserved
        if delta == 0:
            return
        day, month = self._budget_keys(tenant, user)
        pipe = self.r.pipeline()
        pipe.incrby(day, delta)
        pipe.incrby(month, delta)
        pipe.execute()

    def used(self, tenant: str, user: str) -> tuple[int, int]:
        day, month = self._budget_keys(tenant, user)
        return int(self.r.get(day) or 0), int(self.r.get(month) or 0)

    def acquire_model_slot(self) -> str | None:
        """Cluster-wide cap (from the standard limits): protects the provider quota for everyone."""
        lim = self.limits_for("standard")
        holder = uuid.uuid4().hex
        ok = self._sem(keys=[f"{self.p}:sem:model"],
                       args=[self._now(), lim.max_concurrent_model_calls, lim.lease_s, holder])
        return holder if int(ok) else None

    def release_model_slot(self, holder: str) -> None:
        self.r.zrem(f"{self.p}:sem:model", holder)

    def admit(self, tenant: str, user: str, tier: str, token_estimate: int) -> Decision:
        d = self.check_rate(tenant, user, tier)
        if not d.allowed:
            return d
        return self.reserve_tokens(tenant, user, tier, token_estimate)
