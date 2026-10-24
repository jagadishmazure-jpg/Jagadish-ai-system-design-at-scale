"""Durable job queue on Redis with priorities, bounded backlog, max wait, retries, DLQ,
idempotency keys and separate worker pools (bulkheads).

Each pool has its own sorted set, so a flood of slow "reports" jobs cannot starve "refunds".
Score = priority_band * 1e10 + enqueue_time: lower band first, FIFO inside a band.
A job is leased (moved to a processing hash) when claimed; an expired lease is re-queued, so a
worker that crashes mid-job does not lose it.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass

PRIORITY = {"high": 0, "normal": 1, "low": 2}

CLAIM_LUA = """
local q = KEYS[1]; local proc = KEYS[2]
local now = tonumber(ARGV[1]); local lease = tonumber(ARGV[2])
local items = redis.call('ZRANGE', q, 0, 0)
if #items == 0 then return false end
redis.call('ZREM', q, items[1])
redis.call('ZADD', proc, now + lease, items[1])
return items[1]
"""


class QueueFull(Exception):
    pass


@dataclass
class PoolConfig:
    max_backlog: int = 1000
    max_wait_s: float = 300.0
    max_attempts: int = 3
    lease_s: float = 30.0


class JobQueue:
    def __init__(self, redis, pools: dict[str, PoolConfig], clock=None) -> None:
        self.r = redis
        self.pools = pools
        self.clock = clock
        self._claim = redis.register_script(CLAIM_LUA)

    def _now(self) -> float:
        return self.clock.wall() if self.clock else time.time()

    def enqueue(self, pool: str, payload: dict, idem_key: str, priority: str = "normal") -> tuple[str, bool]:
        """Returns (job_id, created). The same idem_key always maps to the same job."""
        cfg = self.pools[pool]
        existing = self.r.get(f"idem:{pool}:{idem_key}")
        if existing:
            return existing.decode() if isinstance(existing, bytes) else existing, False
        if self.r.zcard(f"q:{pool}") >= cfg.max_backlog:
            raise QueueFull(pool)
        job_id = uuid.uuid4().hex[:12]
        if not self.r.set(f"idem:{pool}:{idem_key}", job_id, nx=True, ex=86400):
            got = self.r.get(f"idem:{pool}:{idem_key}")
            return got.decode() if isinstance(got, bytes) else got, False
        now = self._now()
        self.r.hset(f"job:{job_id}", mapping={"payload": json.dumps(payload), "pool": pool, "attempts": 0,
                                              "enqueued": now, "status": "queued", "idem": idem_key})
        self.r.zadd(f"q:{pool}", {job_id: PRIORITY[priority] * 1e10 + now})
        return job_id, True

    def claim(self, pool: str) -> dict | None:
        cfg = self.pools[pool]
        now = self._now()
        self._requeue_expired(pool, now)
        while True:
            raw = self._claim(keys=[f"q:{pool}", f"proc:{pool}"], args=[now, cfg.lease_s])
            if not raw:
                return None
            job_id = raw.decode() if isinstance(raw, bytes) else raw
            h = {k.decode() if isinstance(k, bytes) else k: v.decode() if isinstance(v, bytes) else v
                 for k, v in self.r.hgetall(f"job:{job_id}").items()}
            if now - float(h["enqueued"]) > cfg.max_wait_s:
                self._dead(pool, job_id, "max_wait_exceeded")
                continue
            attempts = int(h["attempts"]) + 1
            self.r.hset(f"job:{job_id}", mapping={"attempts": attempts, "status": "running"})
            return {"job_id": job_id, "payload": json.loads(h["payload"]), "attempts": attempts,
                    "enqueued": float(h["enqueued"])}

    def ack(self, pool: str, job_id: str, result: dict | None = None) -> None:
        self.r.zrem(f"proc:{pool}", job_id)
        self.r.hset(f"job:{job_id}", mapping={"status": "done", "result": json.dumps(result or {})})

    def nack(self, pool: str, job_id: str, error: str) -> str:
        cfg = self.pools[pool]
        self.r.zrem(f"proc:{pool}", job_id)
        attempts = int(self.r.hget(f"job:{job_id}", "attempts") or 0)
        if attempts >= cfg.max_attempts:
            self._dead(pool, job_id, error)
            return "dead"
        self.r.hset(f"job:{job_id}", mapping={"status": "queued", "last_error": error})
        self.r.zadd(f"q:{pool}", {job_id: PRIORITY["high"] * 1e10 + self._now()})
        return "requeued"

    def _dead(self, pool: str, job_id: str, reason: str) -> None:
        self.r.zrem(f"proc:{pool}", job_id)
        self.r.hset(f"job:{job_id}", mapping={"status": "dead", "last_error": reason})
        self.r.rpush(f"dlq:{pool}", job_id)

    def _requeue_expired(self, pool: str, now: float) -> None:
        for jid in self.r.zrangebyscore(f"proc:{pool}", "-inf", now):
            if self.r.zrem(f"proc:{pool}", jid):
                self.r.zadd(f"q:{pool}", {jid: PRIORITY["high"] * 1e10 + now})

    def status(self, job_id: str) -> str | None:
        s = self.r.hget(f"job:{job_id}", "status")
        return s.decode() if isinstance(s, bytes) else s

    def depth(self, pool: str) -> int:
        return int(self.r.zcard(f"q:{pool}"))

    def oldest_age(self, pool: str) -> float:
        first = self.r.zrange(f"q:{pool}", 0, 0)
        if not first:
            return 0.0
        enq = float(self.r.hget(f"job:{first[0].decode() if isinstance(first[0], bytes) else first[0]}", "enqueued"))
        return max(0.0, self._now() - enq)

    def oldest_age_any(self, pool: str) -> float:
        """Oldest wait across all bands (the head of the zset is the highest priority, not the oldest)."""
        ids = self.r.zrange(f"q:{pool}", 0, -1)
        if not ids:
            return 0.0
        now = self._now()
        return max(now - float(self.r.hget(f"job:{(i.decode() if isinstance(i, bytes) else i)}", "enqueued"))
                   for i in ids)

    def dlq(self, pool: str) -> list[str]:
        return [x.decode() if isinstance(x, bytes) else x for x in self.r.lrange(f"dlq:{pool}", 0, -1)]
