"""Talk 14: auth methods by caller type. Depth lives in Jagadish-agent-auth-security; this stays slim."""
import time

import fakeredis
import jwt
import pytest
from fastapi.testclient import TestClient

from sysdesign.app import build_app
from sysdesign.auth import AuthError, LocalIssuer, Verifier
from sysdesign.ext.authn import (
    ACCESS_TTL_S,
    ApiKeyStore,
    CountingKeys,
    MemorySessionStore,
    MockOIDCProvider,
    RedisSessionStore,
    TokenService,
    _h,
)


def test_missing_credentials_401_and_missing_api_key_header_400(issuer):
    ks = ApiKeyStore()
    key = ks.issue("partner-carrier", ["webhooks:write"], ttl_s=3600)
    assert key not in str(ks.rows)  # only the sha256 is stored
    c = TestClient(build_app(redis=fakeredis.FakeRedis(), issuer=issuer, api_keys=ks))
    assert c.post("/v1/chat", json={"message": "hi"}).status_code == 401
    assert c.post("/v1/webhooks/partner", json={}).status_code == 400
    body = {"order_id": "KG-100001", "status": "delivered"}
    assert c.post("/v1/webhooks/partner", json=body, headers={"X-API-Key": "kgk_wrong"}).status_code == 401
    assert c.post("/v1/webhooks/partner", json=body, headers={"X-API-Key": key}).status_code == 200
    ks.revoke(key)  # leaked -> revoked
    assert c.post("/v1/webhooks/partner", json=body, headers={"X-API-Key": key}).status_code == 401
    # a customer JWT is not a partner credential
    tok = issuer.mint("u-ava")
    assert c.post("/v1/webhooks/partner", json=body, headers={"Authorization": f"Bearer {tok}"}).status_code == 400


def test_api_key_scope_and_expiry():
    now = [1000.0]
    ks = ApiKeyStore(clock=lambda: now[0])
    k = ks.issue("p", ["webhooks:write"], ttl_s=10)
    assert ks.verify(k, "webhooks:write") and ks.verify(k, "admin") is None
    now[0] += 11
    assert ks.verify(k, "webhooks:write") is None


def test_redis_sessions_survive_restart_memory_does_not():
    server = fakeredis.FakeServer()
    sid = RedisSessionStore(fakeredis.FakeRedis(server=server)).create("agent-7", ["support"])
    restarted = RedisSessionStore(fakeredis.FakeRedis(server=server))  # new process, same Redis
    assert restarted.get(sid)["user"] == "agent-7"
    assert fakeredis.FakeRedis(server=server).ttl("sess:" + _h(sid)) > 0
    mem = MemorySessionStore()
    msid = mem.create("agent-7", ["support"])
    assert MemorySessionStore().get(msid) is None  # restart = new dict = session gone


def test_jwt_15_min_verified_locally_without_db_and_expiry(issuer):
    ts = TokenService(issuer)
    access, _ = ts.login("u-ava")
    assert ACCESS_TTL_S == 900
    assert jwt.decode(access, options={"verify_signature": False})["exp"] - int(time.time()) <= ACCESS_TTL_S
    keys = CountingKeys({issuer.kid: issuer.public_key})
    CountingKeys.lookups = 0
    for _ in range(5):
        Verifier(keys).verify(access)
    assert ts.db_lookups == 0 and CountingKeys.lookups == 5  # in-memory key map only, no DB
    expired = issuer.mint("u-ava", ttl=-30)
    with pytest.raises(AuthError):
        Verifier({issuer.kid: issuer.public_key}).verify(expired)
    c = TestClient(build_app(redis=fakeredis.FakeRedis(), issuer=issuer))
    assert c.post("/v1/chat", json={"message": "hi"}, headers={"Authorization": f"Bearer {expired}"}).status_code == 401


def test_refresh_cookie_rotation_and_reuse():
    ts = TokenService(LocalIssuer())
    _, r1 = ts.login("u-ava")
    c = ts.cookie(r1)
    assert "HttpOnly" in c and "Secure" in c and "SameSite=Strict" in c
    _, r2 = ts.rotate(r1)
    assert r2 != r1
    with pytest.raises(PermissionError):
        ts.rotate(r1)  # reuse of the old token
    with pytest.raises(PermissionError):
        ts.rotate(r2)  # whole family revoked after reuse


def test_oidc_id_token_checks_and_access_token_is_not_identity():
    idp = MockOIDCProvider()
    toks = idp.authorize("u-ava", nonce="n-123")
    assert idp.identity_from(toks, "n-123")["sub"] == "u-ava"
    with pytest.raises(PermissionError):
        idp.identity_from({"access_token": toks["access_token"]}, "n-123")
    with pytest.raises(PermissionError):
        idp.identity_from(toks, "other-nonce")
    with pytest.raises(jwt.InvalidAudienceError):
        idp.verify_id_token(toks["id_token"], "n-123", client_id="someone-else")
    with pytest.raises(jwt.ExpiredSignatureError):
        idp.verify_id_token(idp.authorize("u-ava", "n", ttl_s=-5)["id_token"], "n")
    forged = MockOIDCProvider()
    forged.signer.kid = idp.signer.kid
    with pytest.raises(jwt.InvalidSignatureError):
        idp.verify_id_token(forged.authorize("u-ava", "n")["id_token"], "n")
    bad_iss = MockOIDCProvider()
    bad_iss.signer, bad_iss.iss = idp.signer, "https://evil.example"
    with pytest.raises(jwt.InvalidIssuerError):
        idp.verify_id_token(bad_iss.authorize("u-ava", "n")["id_token"], "n")
