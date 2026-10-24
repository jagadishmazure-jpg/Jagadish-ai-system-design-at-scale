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


def test_real_postgres_concurrent_startup_does_not_race():
    """Regression: replicas starting together raced on CREATE TABLE IF NOT EXISTS (seen in compose smoke)."""
    if not pg:
        pytest.skip("POSTGRES_DSN not set")
    import threading

    import psycopg

    with psycopg.connect(pg, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS checkpoints")
    errors = []

    def start():
        try:
            CheckpointStore.postgres(pg)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=start) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
