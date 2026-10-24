import itertools

import pytest

from sysdesign.app import run_worker_once
from sysdesign.checkpoints import CheckpointStore
from sysdesign.jobqueue import JobQueue, QueueFull
from sysdesign.tools import Commerce
from sysdesign.workflow import Crash, run_refund


def q(r, clock, **kw):
    ids = itertools.count()
    return JobQueue(r, clock=clock, id_factory=lambda: f"j{next(ids):04d}", **kw)


def test_priority_order(r, clock):
    jq = q(r, clock)
    a = jq.enqueue("bulk", {"n": 1}, "low")
    clock.advance(1)
    b = jq.enqueue("bulk", {"n": 2}, "high")
    clock.advance(1)
    c = jq.enqueue("bulk", {"n": 3}, "normal")
    assert [jq.claim("bulk").job_id for _ in range(3)] == [b, c, a]


def test_fifo_within_priority(r, clock):
    jq = q(r, clock)
    ids = []
    for i in range(3):
        ids.append(jq.enqueue("bulk", {"i": i}))
        clock.advance(0.1)
    assert [jq.claim("bulk").job_id for _ in range(3)] == ids


def test_bounded_backlog(r, clock):
    jq = q(r, clock, pools={"p": {"max_backlog": 2, "max_wait_s": 10}})
    jq.enqueue("p", {})
    jq.enqueue("p", {})
    with pytest.raises(QueueFull):
        jq.enqueue("p", {})


def test_idempotency_key_dedupes(r, clock):
    jq = q(r, clock)
    a = jq.enqueue("interactive", {"x": 1}, idempotency_key="k1")
    b = jq.enqueue("interactive", {"x": 1}, idempotency_key="k1")
    assert a == b and jq.depth("interactive") == 1


def test_max_wait_expires_to_dlq(r, clock):
    jq = q(r, clock, pools={"p": {"max_backlog": 10, "max_wait_s": 5}})
    jq.enqueue("p", {})
    clock.advance(6)
    assert jq.claim("p") is None
    assert jq.dlq("p")[0]["reason"] == "expired"


def test_poison_job_dead_letters_after_max_attempts(r, clock):
    jq = q(r, clock, max_attempts=2)
    jid = jq.enqueue("bulk", {})
    jq.nack(jq.claim("bulk"))
    jq.nack(jq.claim("bulk"))
    assert jq.claim("bulk") is None and jq.status(jid) == "dead:error"


def test_lease_expiry_requeues(r, clock):
    jq = q(r, clock, lease_s=10)
    jid = jq.enqueue("bulk", {})
    jq.claim("bulk")  # worker dies holding it
    clock.advance(11)
    assert jq.reap("bulk") == 1
    assert jq.claim("bulk").job_id == jid


def test_bulkheads_isolate_pools(r, clock):
    jq = q(r, clock)
    for _ in range(50):
        jq.enqueue("bulk", {})
    i = jq.enqueue("interactive", {})
    assert jq.claim("interactive").job_id == i


def test_unknown_pool_or_priority(r, clock):
    jq = q(r, clock)
    with pytest.raises(ValueError):
        jq.enqueue("nope", {})
    with pytest.raises(ValueError):
        jq.enqueue("bulk", {}, priority="urgent!!")


def test_oldest_age(r, clock):
    jq = q(r, clock)
    jq.enqueue("bulk", {})
    clock.advance(7)
    jq.enqueue("bulk", {})
    assert jq.oldest_age("bulk") == pytest.approx(7)


def test_checkpoint_roundtrip():
    s = CheckpointStore.sqlite()
    s.save("w1", 2, {"a": 1}, "running", "r1")
    assert s.load("w1") == {"step": 2, "state": {"a": 1}, "status": "running", "owner": "r1"}
    assert s.load("missing") is None


def test_chaos_kill_replica_mid_workflow_resumes_and_refunds_once(r, clock):
    """Replica r1 dies after check_policy; r2 resumes from the checkpoint, refund issued exactly once."""
    store, shop, jq = CheckpointStore.sqlite(), Commerce(), q(r, clock, lease_s=30)
    jid = jq.enqueue("interactive", {"kind": "refund", "user_id": "u-ava", "order_id": "KG-100002", "amount": 20.0})
    with pytest.raises(Crash):
        run_worker_once(jq, store, shop, "r1", crash_after="check_policy")
    assert store.load(jid)["step"] == 2 and shop.refunds == {}
    clock.advance(31)
    jq.reap("interactive")
    out = run_worker_once(jq, store, shop, "r2")
    assert out["status"] == "done"
    assert out["state"]["log"] == ["verify_order@r1", "check_policy@r1", "issue_refund@r2", "notify@r2"]
    assert len(shop.refunds) == 1


def test_crash_after_refund_before_ack_does_not_double_refund(r, clock):
    store, shop = CheckpointStore.sqlite(), Commerce()
    with pytest.raises(Crash):
        run_refund(store, shop, "wf", "u-ava", "KG-100002", 20.0, "r1", crash_after="issue_refund")
    run_refund(store, shop, "wf", "u-ava", "KG-100002", 20.0, "r2")
    run_refund(store, shop, "wf", "u-ava", "KG-100002", 20.0, "r3")  # replay after done is a no-op
    assert len(shop.refunds) == 1


def test_workflow_rejects_other_users_order():
    store, shop = CheckpointStore.sqlite(), Commerce()
    out = run_refund(store, shop, "wf", "u-ava", "KG-100003", 5.0, "r1")
    assert out["status"] == "rejected" and shop.refunds == {}


def test_workflow_rejects_over_refund():
    store, shop = CheckpointStore.sqlite(), Commerce()
    assert run_refund(store, shop, "a", "u-ava", "KG-100002", 50.0, "r1")["status"] == "done"
    assert run_refund(store, shop, "b", "u-ava", "KG-100002", 10.0, "r1")["status"] == "rejected"


def test_requeued_started_job_is_not_expired_by_max_wait(r, clock):
    # regression: a job resumed after a crash was dead-lettered as "expired" because its age kept counting
    jq = q(r, clock, pools={"p": {"max_backlog": 10, "max_wait_s": 5}}, lease_s=2)
    jid = jq.enqueue("p", {})
    jq.claim("p")
    clock.advance(10)
    jq.reap("p")
    assert jq.claim("p").job_id == jid
