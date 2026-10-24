"""Durable job queue on Redis: priorities, bounded backlog, max wait, leases, DLQ, idempotency,
and separate pools (bulkheads) so a flood of bulk jobs cannot starve interactive ones.

Layout per pool:
  q:{pool}:ready     ZSET  score = priority * 1e12 + enqueue_time  (lower = sooner)
  q:{pool}:inflight  ZSET  score = lease expiry
  q:{pool}:dlq       LIST  job ids with a reason
  q:job:{id}         HASH  payload, pool, attempts, enqueued, status
  q:idem:{key}       STR   job id (SET NX) so retries of the same request enqueue once
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass

POOLS = {"interactive": {"max_backlog": 200, "max_wait_s": 30.0},
         "bulk": {"max_backlog": 2000, "max_wait_s": 900.0}}
PRIORITY = {"high": 0, "normal": 1, "low": 2}

ENQUEUE_LUA = """
-- KEYS: ready, idem, job ; ARGV: id, score, max_backlog, payload_json, pool, now, idem_ttl
if KEYS[2] ~= '' then
  local existing = redis.call('GET', KEYS[2])
  if existing then return {'dup', existing} end
end
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[3]) then return {'full', ''} end
redis.call('HSET', KEYS[3], 'payload', ARGV[4], 'pool', ARGV[5], 'attempts', 0, 'enqueued', ARGV[6],
           'status', 'queued')
redis.call('ZADD', KEYS[1], tonumber(ARGV[2]), ARGV[1])
if KEYS[2] ~= '' then redis.call('SET', KEYS[2], ARGV[1], 'EX', tonumber(ARGV[7])) end
return {'ok', ARGV[1]}
"""

CLAIM_LUA = """
-- KEYS: ready, inflight ; ARGV: now, lease
local ids = redis.call('ZRANGE', KEYS[1], 0, 0)
if #ids == 0 then return false end
redis.call('ZREM', KEYS[1], ids[1])
redis.call('ZADD', KEYS[2], tonumber(ARGV[1]) + tonumber(ARGV[2]), ids[1])
return ids[1]
"""


class QueueFull(Exception):
    pass


@dataclass
class Job:
    job_id: str
    pool: str
    payload: dict
    attempts: int
    enqueued: float


class JobQueue:
    def __init__(self, redis, clock=None, pools: dict | None = None, lease_s: float = 30.0,
                 max_attempts: int = 3, prefix: str = "q", id_factory=None) -> None:
        self.r = redis
        self.clock = clock
        self.pools = pools or POOLS
        self.lease_s = lease_s
        self.max_attempts = max_attempts
        self.p = prefix
        self._new_id = id_factory or (lambda: uuid.uuid4().hex)
        self._enqueue = redis.register_script(ENQUEUE_LUA)
        self._claim = redis.register_script(CLAIM_LUA)

    def _now(self) -> float:
        return self.clock.now() if self.clock else time.time()

    def _k(self, pool: str, part: str) -> str:
        return f"{self.p}:{pool}:{part}"

    def enqueue(self, pool: str, payload: dict, priority: str = "normal", idempotency_key: str | None = None) -> str:
        if pool not in self.pools:
            raise ValueError(f"unknown pool {pool}")
        if priority not in PRIORITY:
            raise ValueError(f"unknown priority {priority}")
        job_id = self._new_id()
        now = self._now()
        idem = f"{self.p}:idem:{pool}:{idempotency_key}" if idempotency_key else ""
        status, jid = self._enqueue(
            keys=[self._k(pool, "ready"), idem, f"{self.p}:job:{job_id}"],
            args=[job_id, PRIORITY[priority] * 1e12 + now, self.pools[pool]["max_backlog"],
                  json.dumps(payload), pool, now, 86400])
        status = status.decode() if isinstance(status, bytes) else status
        jid = jid.decode() if isinstance(jid, bytes) else jid
        if status == "full":
            raise QueueFull(pool)
        return jid

    def _load(self, job_id: str) -> Job:
        h = {k.decode(): v.decode() for k, v in self.r.hgetall(f"{self.p}:job:{job_id}").items()}
        return Job(job_id, h["pool"], json.loads(h["payload"]), int(h["attempts"]), float(h["enqueued"]))

    def claim(self, pool: str) -> Job | None:
        """Claim the next job; jobs past max wait go to the DLQ instead of being run late."""
        while True:
            jid = self._claim(keys=[self._k(pool, "ready"), self._k(pool, "inflight")],
                              args=[self._now(), self.lease_s])
            if not jid:
                return None
            jid = jid.decode() if isinstance(jid, bytes) else jid
            job = self._load(jid)
            # max wait applies to work that never started; a job resumed after a crash must finish
            if job.attempts == 0 and self._now() - job.enqueued > self.pools[pool]["max_wait_s"]:
                self._dead(job, "expired")
                continue
            self.r.hincrby(f"{self.p}:job:{jid}", "attempts", 1)
            self.r.hset(f"{self.p}:job:{jid}", "status", "running")
            job.attempts += 1
            return job

    def ack(self, job: Job) -> None:
        self.r.zrem(self._k(job.pool, "inflight"), job.job_id)
        self.r.hset(f"{self.p}:job:{job.job_id}", "status", "done")

    def nack(self, job: Job, reason: str = "error") -> None:
        self.r.zrem(self._k(job.pool, "inflight"), job.job_id)
        if job.attempts >= self.max_attempts:
            self._dead(job, reason)
        else:
            self.r.hset(f"{self.p}:job:{job.job_id}", "status", "queued")
            self.r.zadd(self._k(job.pool, "ready"), {job.job_id: PRIORITY["normal"] * 1e12 + job.enqueued})

    def _dead(self, job: Job, reason: str) -> None:
        self.r.zrem(self._k(job.pool, "inflight"), job.job_id)
        self.r.hset(f"{self.p}:job:{job.job_id}", "status", f"dead:{reason}")
        self.r.rpush(self._k(job.pool, "dlq"), json.dumps({"job_id": job.job_id, "reason": reason}))

    def reap(self, pool: str) -> int:
        """Return jobs whose lease expired (worker died) to the ready set, or DLQ them if out of attempts."""
        now = self._now()
        n = 0
        for jid in self.r.zrangebyscore(self._k(pool, "inflight"), "-inf", now):
            jid = jid.decode() if isinstance(jid, bytes) else jid
            job = self._load(jid)
            self.nack(job, "lease_expired")
            n += 1
        return n

    def status(self, job_id: str) -> str | None:
        v = self.r.hget(f"{self.p}:job:{job_id}", "status")
        return v.decode() if v else None

    def depth(self, pool: str) -> int:
        return int(self.r.zcard(self._k(pool, "ready")))

    def oldest_age(self, pool: str) -> float:
        ids = self.r.zrange(self._k(pool, "ready"), 0, -1)
        if not ids:
            return 0.0
        now = self._now()
        ages = [now - float(self.r.hget(f"{self.p}:job:{(i.decode() if isinstance(i, bytes) else i)}", "enqueued"))
                for i in ids]
        return max(ages)

    def dlq(self, pool: str) -> list[dict]:
        return [json.loads(x) for x in self.r.lrange(self._k(pool, "dlq"), 0, -1)]
