"""Runs only when REDIS_URL / POSTGRES_DSN point at real servers (CI integration job, docker compose)."""
import itertools
import os

import pytest

from sysdesign.admission import Admission, Limits
from sysdesign.checkpoints import CheckpointStore
from sysdesign.jobqueue import JobQueue

redis_url = os.environ.get("REDIS_URL")
pg = os.environ.get("POSTGRES_DSN")


@pytest.fixture
def real_redis():
    if not redis_url:
        pytest.skip("REDIS_URL not set")
    import redis

    r = redis.Redis.from_url(redis_url)
    r.flushdb()
    return r


def test_real_redis_token_bucket_shared(real_redis):
    a1 = Admission(real_redis, limits={"standard": Limits(rate_per_s=0.001, burst=3)})
    a2 = Admission(real_redis, limits={"standard": Limits(rate_per_s=0.001, burst=3)})
    got = [(a1, a2)[i % 2].check_rate("t", "u", "standard").allowed for i in range(6)]
    assert got.count(True) == 3


def test_real_redis_queue_roundtrip(real_redis):
    ids = itertools.count()
    q = JobQueue(real_redis, id_factory=lambda: f"j{next(ids)}")
    jid = q.enqueue("bulk", {"x": 1}, idempotency_key="k")
    assert q.enqueue("bulk", {"x": 1}, idempotency_key="k") == jid
    job = q.claim("bulk")
    q.ack(job)
    assert q.status(jid) == "done"


def test_real_postgres_checkpoints():
    if not pg:
        pytest.skip("POSTGRES_DSN not set")
    s = CheckpointStore.postgres(pg)
    s.save("wf-int", 1, {"a": 1})
    s.save("wf-int", 2, {"a": 2}, "done")
    assert s.load("wf-int")["step"] == 2
