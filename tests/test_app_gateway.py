import fakeredis
import pytest
from fastapi.testclient import TestClient

from sysdesign.admission import Limits
from sysdesign.app import MAX_BODY, build_app
from sysdesign.auth import LocalIssuer


@pytest.fixture
def env(issuer):
    r = fakeredis.FakeRedis()
    app = build_app(redis=r, issuer=issuer)
    return TestClient(app), issuer, app


def h(issuer, sub="u-ava", **kw):
    return {"authorization": f"Bearer {issuer.mint(sub, **kw)}"}


def test_health_is_public(env):
    c, *_ = env
    assert c.get("/healthz").json()["ok"] and c.get("/readyz").json()["ready"]


def test_missing_token_401(env):
    c, *_ = env
    r = c.post("/v1/chat", json={"message": "hi"})
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"


def test_forged_token_401(env):
    c, *_ = env
    r = c.post("/v1/chat", json={"message": "hi"}, headers=h(LocalIssuer()))
    assert r.status_code == 401


def test_chat_ok_with_request_id(env):
    c, iss, _ = env
    r = c.post("/v1/chat", json={"message": "hi"}, headers={**h(iss), "x-request-id": "abc12345-req"})
    assert r.status_code == 200 and r.json()["rung"] == "rules" and r.headers["x-request-id"] == "abc12345-req"


def test_unsafe_request_id_replaced(env):
    c, iss, _ = env
    r = c.post("/v1/chat", json={"message": "hi"}, headers={**h(iss), "x-request-id": "<script>"})
    assert r.headers["x-request-id"] != "<script>" and len(r.headers["x-request-id"]) == 32


def test_oversized_body_413_before_auth(env):
    c, *_ = env
    r = c.post("/v1/chat", content=b"x" * (MAX_BODY + 1), headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_unknown_route_404(env):
    c, iss, _ = env
    assert c.get("/internal/admin", headers=h(iss)).status_code == 404


def test_admin_requires_scope(env):
    c, iss, _ = env
    body = {"doc_id": "returns", "paragraphs": ["x"]}
    assert c.post("/v1/admin/policy", json=body, headers=h(iss)).status_code == 403
    ok = c.post("/v1/admin/policy", json=body, headers=h(iss, scopes=("chat", "policy:write")))
    assert ok.status_code == 200 and "returns#0" in ok.json()["changed_chunks"]


def test_429_has_retry_after(issuer):
    app = build_app(redis=fakeredis.FakeRedis(), issuer=issuer)
    app.state.agent.admission.limits = {"standard": Limits(rate_per_s=0.01, burst=1)}
    c = TestClient(app)
    c.post("/v1/chat", json={"message": "hi"}, headers=h(issuer))
    r = c.post("/v1/chat", json={"message": "hi"}, headers=h(issuer))
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1


def test_rate_limit_shared_by_replicas(issuer):
    r = fakeredis.FakeRedis()
    reps = [build_app(redis=r, issuer=issuer, replica=f"r{i}") for i in range(3)]
    for a in reps:
        a.state.agent.admission.limits = {"standard": Limits(rate_per_s=0.001, burst=3)}
    clients = [TestClient(a) for a in reps]
    codes = [clients[i % 3].post("/v1/chat", json={"message": "hi"}, headers=h(issuer)).status_code
             for i in range(6)]
    assert codes.count(200) == 3 and codes.count(429) == 3


def test_bad_json_422(env):
    c, iss, _ = env
    r = c.post("/v1/chat", content=b"{nope", headers={**h(iss), "content-type": "application/json"})
    assert r.status_code == 422


def test_sse_stream_tokens_and_ttft_metric(env):
    c, iss, app = env
    with c.stream("POST", "/v1/chat/stream", json={"message": "hi"}, headers=h(iss)) as r:
        body = "".join(r.iter_text())
    assert r.headers["content-type"].startswith("text/event-stream")
    assert body.count("event: token") >= 3 and "event: done" in body
    assert "sd_time_to_first_token_seconds_count 1.0" in c.get("/metrics").text


def test_refund_job_idempotent_and_owner_scoped(env):
    c, iss, app = env
    hdr = {**h(iss), "idempotency-key": "refund-key-0001"}
    j1 = c.post("/v1/refunds", json={"order_id": "KG-100002", "amount": 10}, headers=hdr).json()["job_id"]
    j2 = c.post("/v1/refunds", json={"order_id": "KG-100002", "amount": 10}, headers=hdr).json()["job_id"]
    assert j1 == j2
    assert c.get(f"/v1/jobs/{j1}", headers=h(iss)).json()["status"] == "queued"
    assert c.get(f"/v1/jobs/{j1}", headers=h(iss, sub="u-ben")).status_code == 404


def test_refund_idempotency_keys_scoped_per_user(env):
    c, iss, _ = env
    a = c.post("/v1/refunds", json={"order_id": "KG-100002", "amount": 10},
               headers={**h(iss), "idempotency-key": "same-key-0001"}).json()["job_id"]
    b = c.post("/v1/refunds", json={"order_id": "KG-100003", "amount": 10},
               headers={**h(iss, sub="u-ben"), "idempotency-key": "same-key-0001"}).json()["job_id"]
    assert a != b  # one user's key must not return another user's job


@pytest.mark.parametrize("amount", [0, -5, "nan", "inf"])
def test_refund_rejects_bad_amounts(env, amount):
    c, iss, _ = env
    r = c.post("/v1/refunds", json={"order_id": "KG-100002", "amount": amount},
               headers={**h(iss), "idempotency-key": "k-bad-amount"})
    assert r.status_code == 422


def test_refund_requires_idempotency_key(env):
    c, iss, _ = env
    assert c.post("/v1/refunds", json={"order_id": "KG-100002", "amount": 5}, headers=h(iss)).status_code == 422


def test_metrics_exposed(env):
    c, iss, _ = env
    c.post("/v1/chat", json={"message": "hi"}, headers=h(iss))
    assert "sd_requests_total" in c.get("/metrics").text


def test_admin_bad_json_is_422_not_500(env):
    c, iss, _ = env
    hdr = {**h(iss, scopes=("chat", "policy:write")), "content-type": "application/json"}
    assert c.post("/v1/admin/policy", content=b"[1,2", headers=hdr).status_code == 422
    assert c.post("/v1/admin/policy", content=b"[1,2]", headers=hdr).status_code == 422
