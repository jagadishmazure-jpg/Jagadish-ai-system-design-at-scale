import json
import sqlite3

import fakeredis
import pytest

from sysdesign import backend
from sysdesign.admission import AdmissionController, Limits
from sysdesign.autoscaler import Autoscaler
from sysdesign.clock import FakeClock
from sysdesign.jobqueue import JobQueue, PoolConfig, QueueFull
from sysdesign.llm import default_providers
from sysdesign.resilience import CircuitBreaker, Degraded, ModelGateway
from sysdesign.runtime import InstanceKilled
from sysdesign.store import Store
from sysdesign.workflows import refund_flow
from tests.conftest import client_for, run


def test_token_bucket_across_replicas_shares_redis():
    r = fakeredis.FakeRedis()
    a = AdmissionController(r, Limits(user_rps=1, user_burst=3))
    b = AdmissionController(r, Limits(user_rps=1, user_burst=3))  # second replica
    got = [(a if i % 2 else b).admit("t", "u", 10) for i in range(6)]
    assert sum(d.allowed for d in got) == 3
    denied = [d for d in got if not d.allowed][0]
    assert denied.retry_after_s >= 1


def test_negative_estimate_rejected_and_release_idempotent():
    adm = AdmissionController(fakeredis.FakeRedis())
    assert adm.admit("t", "u", -5).reason == "invalid_estimate"
    d = adm.admit("t", "u", 100)
    adm.release(d.reservation, 80)
    adm.release(d.reservation, 80)
    assert adm.usage("t")["daily"] == 80


def test_daily_budget_and_concurrency_cap():
    adm = AdmissionController(fakeredis.FakeRedis(), Limits(tenant_daily_tokens=150, max_concurrent_model_calls=1,
                                                               user_burst=100, user_rps=100))
    d1 = adm.admit("t", "u1", 100)
    assert d1.allowed
    assert adm.admit("t", "u2", 10).reason == "concurrency"
    adm.release(d1.reservation, 100)
    assert adm.admit("t", "u2", 100).reason == "daily_token_budget"


def test_breaker_opens_and_fallback_serves():
    p = default_providers()
    p["large"].outage = True
    gw = ModelGateway(p)
    for _ in range(6):
        out = run(gw.complete("large", "explain shipping"))
        assert out
    assert gw.breaker("large").state == "open" and gw.fallbacks_used > 0
    for v in p.values():
        v.outage = True
    with pytest.raises(Degraded):
        run(gw.complete("large", "x"))


def test_breaker_half_open_recovers():
    c = FakeClock()
    b = CircuitBreaker(failure_threshold=2, cooldown_s=5, clock=c)
    b.failure()
    b.failure()
    assert not b.allow()
    c.advance(6)
    assert b.allow()
    b.success()
    assert b.state == "closed"


def test_queue_priority_idempotency_bound_dlq():
    q = JobQueue(fakeredis.FakeRedis(), {"p": PoolConfig(max_backlog=3, max_attempts=1)})
    j1, new1 = q.enqueue("p", {}, "k1")
    j1b, new2 = q.enqueue("p", {}, "k1")
    assert j1 == j1b and new1 and not new2
    q.enqueue("p", {}, "k2", priority="high")
    assert q.claim("p")["job_id"] != j1  # high first
    q.enqueue("p", {}, "k3")
    q.enqueue("p", {}, "k4")
    with pytest.raises(QueueFull):
        q.enqueue("p", {}, "k5")
    j = q.claim("p")
    q.nack("p", j["job_id"], "boom")
    assert j["job_id"] in q.dlq("p")


def test_autoscaler_depth_age_and_warm_min():
    a = Autoscaler(warm_min=2, max_workers=10, target_jobs_per_worker=10, max_age_s=5, scale_down_stable_s=30)
    assert a.decide(0, 0, 0) == 2
    assert a.decide(1, 75, 0) == 8
    assert a.decide(2, 0, 0) == 8  # does not flap down immediately
    assert a.decide(40, 0, 0) == 2
    assert a.decide(41, 5, 9) == 4  # old task forces scale-up even with shallow queue


def test_kill_instance_resume_from_checkpoint_on_other_replica():
    store, clock = Store(":memory:"), FakeClock()
    inputs = {"order_id": "QM-10001", "owner": "u-ana"}
    rt = refund_flow(store, clock, kill_after="issue_refund", approval_required=False)
    rt.start("wf-1", "u-ana", inputs)
    with pytest.raises(InstanceKilled):
        run(rt.run("wf-1", "u-ana"))
    rt2 = refund_flow(store, clock, replica="api-2", approval_required=False)
    assert run(rt2.run("wf-1", "u-ana")) == "completed"
    assert len(store.effects("wf:wf-1:refund")) == 1  # not refunded twice


def test_saga_compensates_in_reverse():
    store, clock = Store(":memory:"), FakeClock()
    rt = refund_flow(store, clock, pickup_fails=True, approval_required=False)
    rt.start("wf-2", "u-ana", {"order_id": "QM-10001", "owner": "u-ana"})
    assert run(rt.run("wf-2", "u-ana")) == "compensated"
    keys = [e["key"] for e in store.effects("wf:wf-2")]
    assert keys.index("wf:wf-2:label_void") < keys.index("wf:wf-2:refund_reversal")


def test_refund_and_record_atomic(tmp_path):
    db = str(tmp_path / "t.db")
    with pytest.raises(backend.CrashBetweenWrites):
        backend.refund_with_record(db, "QM-10001", 59.99, crash_between=True)
    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM order_state").fetchone()[0] == 0
    backend.refund_with_record(db, "QM-10001", 59.99)
    assert c.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 1


def test_index_changes_plan_and_speeds_lookup():
    r = backend.index_proof(rows=20_000, lookups=300)
    assert r["plan_before"].startswith("SCAN") and "USING INDEX" in r["plan_after"]
    assert r["ms_per_lookup_after"] < r["ms_per_lookup_before"]


def test_attachment_bytes_in_object_store_only_link_in_db(tmp_path):
    objs = backend.LocalObjectStore(tmp_path / "obj", secret=b"k")
    store = Store(":memory:")
    photo = b"\x89PNG fake damaged-item photo" * 100
    link = objs.put_object("attachments", "QM-10001/photo.png", photo)
    store.remember("u-ana", "attachment:QM-10001", link)
    saved = store.recall("u-ana")["attachment:QM-10001"]
    assert saved == "s3://attachments/QM-10001/photo.png" and len(saved) < 100
    assert objs.get_object("attachments", "QM-10001/photo.png") == photo
    url = objs.signed_url("attachments", "QM-10001/photo.png", expires_at=2000)
    sig = url.split("sig=")[1]
    assert objs.verify_url("attachments", "QM-10001/photo.png", 2000, sig, now=1000)
    assert not objs.verify_url("attachments", "QM-10001/photo.png", 2000, sig, now=3000)
    with pytest.raises(ValueError):
        objs.put_object("attachments", "../escape", b"x")


def test_in_memory_state_lost_on_restart_db_survives(tmp_path):
    mem = backend.InProcessSessionStore()
    mem.put("cart:u-ana", "QM-10001")
    db = str(tmp_path / "s.db")
    Store(db).remember("u-ana", "cart", "QM-10001")
    mem = backend.InProcessSessionStore()  # restart
    assert mem.get("cart:u-ana") is None
    assert Store(db).recall("u-ana")["cart"] == "QM-10001"


def test_readiness_fails_on_broken_config(app, monkeypatch):
    monkeypatch.setenv("APP_BROKEN_CONFIG", "1")

    async def go():
        async with client_for(app) as c:
            return (await c.get("/readyz")).status_code, (await c.get("/healthz")).status_code
    assert run(go()) == (503, 200)  # alive but not ready: k8s keeps it out of rotation


def test_live_order_push_over_sse(system, issuer):
    """Real uvicorn server on a loopback port (ASGITransport buffers whole bodies, so it can't stream)."""
    import socket
    import threading
    import time

    import httpx
    import uvicorn

    from sysdesign.app import create_app
    from sysdesign.auth import ApiKeyStore
    ks = ApiKeyStore()
    key = ks.issue("carrier", ["webhooks:write"], 3600)
    app = create_app(system, issuer, api_keys=ks)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    while not server.started:
        time.sleep(0.02)
    events = []
    try:
        def listen():
            with httpx.stream("GET", f"http://127.0.0.1:{port}/v1/orders/QM-10002/events", timeout=5,
                              headers={"Authorization": "Bearer " + issuer.token("u-ana")}) as r:
                for line in r.iter_lines():
                    if line.startswith("data: "):
                        events.append(json.loads(line[6:]))
        lt = threading.Thread(target=listen)
        lt.start()
        deadline = time.time() + 5
        while not system.order_events.subs.get("QM-10002") and time.time() < deadline:
            time.sleep(0.01)
        r = httpx.post(f"http://127.0.0.1:{port}/v1/webhooks/partner", headers={"X-API-Key": key},
                       json={"order_id": "QM-10002", "status": "delivered"})
        assert r.json()["pushed_to"] == 1
        lt.join(5)
    finally:
        server.should_exit = True
        th.join(5)
    assert [e["status"] for e in events] == ["in_transit", "delivered"]
