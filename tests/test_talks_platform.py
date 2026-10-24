"""Talks 3, 5, 6, 12: backend choices, readiness for rollouts, live order push. Uses main's app and orders."""
import json
import socket
import sqlite3
import threading
import time

import fakeredis
import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from sysdesign.app import build_app
from sysdesign.ext import backend
from sysdesign.ext.authn import ApiKeyStore


def test_refund_and_record_atomic(tmp_path):
    db = str(tmp_path / "t.db")
    with pytest.raises(backend.CrashBetweenWrites):
        backend.refund_with_record(db, "KG-100002", 58.50, crash_between=True)
    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM order_state").fetchone()[0] == 0
    backend.refund_with_record(db, "KG-100002", 58.50)
    assert c.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 1


def test_index_changes_plan_and_speeds_lookup():
    r = backend.index_proof(rows=20_000, lookups=300)
    assert r["plan_before"].startswith("SCAN") and "USING INDEX" in r["plan_after"]
    assert r["ms_per_lookup_after"] < r["ms_per_lookup_before"]


def test_attachment_bytes_in_object_store_only_link_in_db(tmp_path):
    objs = backend.LocalObjectStore(tmp_path / "obj", secret=b"k")
    photo = b"\x89PNG fake damaged-item photo" * 100
    link = objs.put_object("attachments", "KG-100002/photo.png", photo)
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE attachments(order_id TEXT, link TEXT)")
    db.execute("INSERT INTO attachments VALUES (?, ?)", ("KG-100002", link))
    saved = db.execute("SELECT link FROM attachments").fetchone()[0]
    assert saved == "s3://attachments/KG-100002/photo.png" and len(saved) < 100
    assert objs.get_object("attachments", "KG-100002/photo.png") == photo
    url = objs.signed_url("attachments", "KG-100002/photo.png", expires_at=2000)
    sig = url.split("sig=")[1]
    assert objs.verify_url("attachments", "KG-100002/photo.png", 2000, sig, now=1000)
    assert not objs.verify_url("attachments", "KG-100002/photo.png", 2000, sig, now=3000)
    with pytest.raises(ValueError):
        objs.put_object("attachments", "../escape", b"x")


def test_in_memory_state_lost_on_restart_db_survives(tmp_path):
    mem = backend.InProcessSessionStore()
    mem.put("cart:u-ava", "KG-100001")
    db = str(tmp_path / "s.db")
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE kv(k TEXT PRIMARY KEY, v TEXT)")
        c.execute("INSERT INTO kv VALUES ('cart:u-ava', 'KG-100001')")
    mem = backend.InProcessSessionStore()  # restart
    assert mem.get("cart:u-ava") is None
    assert sqlite3.connect(db).execute("SELECT v FROM kv").fetchone()[0] == "KG-100001"


def test_readiness_fails_on_broken_config_and_version_is_public(issuer, monkeypatch):
    monkeypatch.setenv("APP_VERSION", "v9")
    c = TestClient(build_app(redis=fakeredis.FakeRedis(), issuer=issuer))
    assert c.get("/version").json()["version"] == "v9"
    monkeypatch.setenv("APP_BROKEN_CONFIG", "1")
    assert (c.get("/readyz").status_code, c.get("/healthz").status_code) == (503, 200)  # alive, not ready


def test_every_response_counted_by_route_and_status(issuer):
    c = TestClient(build_app(redis=fakeredis.FakeRedis(), issuer=issuer))
    c.post("/v1/chat", json={"message": "hi"})  # 401, rejected before the handler
    c.get("/healthz")
    text = c.get("/metrics").text
    assert 'sd_http_requests_total{route="/v1/chat",status="401"} 1.0' in text
    assert 'sd_http_requests_total{route="/healthz",status="200"} 1.0' in text


def test_order_events_ownership(issuer):
    c = TestClient(build_app(redis=fakeredis.FakeRedis(), issuer=issuer))
    ben = {"Authorization": "Bearer " + issuer.mint("u-ben")}
    assert c.get("/v1/orders/KG-100001/events", headers=ben).status_code == 404  # Ava's order
    assert c.get("/v1/orders/KG-999999/events", headers=ben).status_code == 404


def test_live_order_push_over_sse(issuer):
    """Real uvicorn on a loopback port: the in-process test client buffers whole bodies, so it can't stream."""
    ks = ApiKeyStore()
    key = ks.issue("carrier", ["webhooks:write"], 3600)
    app = build_app(redis=fakeredis.FakeRedis(), issuer=issuer, api_keys=ks)
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
            with httpx.stream("GET", f"http://127.0.0.1:{port}/v1/orders/KG-100001/events", timeout=5,
                              headers={"Authorization": "Bearer " + issuer.mint("u-ava")}) as r:
                for line in r.iter_lines():
                    if line.startswith("data: "):
                        events.append(json.loads(line[6:]))
        lt = threading.Thread(target=listen)
        lt.start()
        deadline = time.time() + 5
        while not app.state.order_events.subs.get("KG-100001") and time.time() < deadline:
            time.sleep(0.01)
        r = httpx.post(f"http://127.0.0.1:{port}/v1/webhooks/partner", headers={"X-API-Key": key},
                       json={"order_id": "KG-100001", "status": "delivered"})
        assert r.json()["pushed_to"] == 1
        lt.join(5)
    finally:
        server.should_exit = True
        th.join(5)
    assert [e["status"] for e in events] == ["shipped", "delivered"]
