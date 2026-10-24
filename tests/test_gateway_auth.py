import time

import fakeredis
import jwt
import pytest

from sysdesign.app import create_app
from sysdesign.auth import (
    ACCESS_TTL_S,
    ApiKeyStore,
    CountingKeys,
    MemorySessionStore,
    MockOIDCProvider,
    RedisSessionStore,
    TokenService,
)
from sysdesign.gateway import AUDIENCE, ISSUER, AuthError, LocalIssuer, route_for, verify
from tests.conftest import client_for, run


async def _post(app, path, **kw):
    async with client_for(app) as c:
        return await c.post(path, **kw)


def test_missing_bearer_is_401(app):
    assert run(_post(app, "/v1/chat", json={"message": "hi"})).status_code == 401


def test_request_id_and_413(app, issuer):
    r = run(_post(app, "/v1/chat", json={"message": "hi"}, headers={"Authorization": "Bearer " + issuer.token("u-ana")}))
    assert r.status_code == 200 and len(r.headers["x-request-id"]) == 32
    big = run(_post(app, "/v1/chat", content=b"x" * 20_000, headers={"Authorization": "Bearer x"}))
    assert big.status_code == 413


def test_alg_none_and_unknown_kid_rejected(issuer):
    keys = {issuer.kid: issuer.public_pem()}
    none_tok = jwt.encode({"sub": "x", "iss": ISSUER, "aud": AUDIENCE}, key=None, algorithm="none")
    with pytest.raises(AuthError):
        verify(none_tok, keys)
    other = LocalIssuer(kid="evil")
    with pytest.raises(AuthError):
        verify(other.token("u-ana"), keys)


def test_route_prefix_is_exact():
    assert route_for("/v1/chatty") is None and route_for("/v1/chat/stream") is not None


def test_api_key_missing_header_400_and_revocation():
    ks = ApiKeyStore()
    key = ks.issue("partner-carrier", ["webhooks:write"], ttl_s=3600)
    assert key not in str(ks.rows)  # only hashes stored
    app = create_app(api_keys=ks)
    assert run(_post(app, "/v1/webhooks/partner", json={})).status_code == 400
    ok = run(_post(app, "/v1/webhooks/partner", json={"order_id": "QM-10002", "status": "packed"},
                   headers={"X-API-Key": key}))
    assert ok.status_code == 200
    ks.revoke(key)  # leaked -> revoked
    assert run(_post(app, "/v1/webhooks/partner", json={"order_id": "QM-10002", "status": "packed"},
                     headers={"X-API-Key": key})).status_code == 401


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
    restarted = RedisSessionStore(fakeredis.FakeRedis(server=server))  # new gateway process, same Redis
    assert restarted.get(sid)["user"] == "agent-7"
    assert server.connected and fakeredis.FakeRedis(server=server).ttl("sess:" + __import__("hashlib")
                                                                          .sha256(sid.encode()).hexdigest()) > 0
    mem = MemorySessionStore()
    msid = mem.create("agent-7", ["support"])
    assert MemorySessionStore().get(msid) is None  # process restart = new dict = session gone


def test_jwt_verified_locally_without_db_and_expiry():
    iss = LocalIssuer()
    ts = TokenService(iss)
    access, _ = ts.login("u-ana")
    assert jwt.decode(access, options={"verify_signature": False})["exp"] - int(time.time()) <= ACCESS_TTL_S
    keys = CountingKeys({iss.kid: iss.public_pem()})
    CountingKeys.lookups = 0
    for _ in range(5):
        verify(access, keys)
    assert ts.db_lookups == 0 and CountingKeys.lookups == 5  # in-memory key map only
    expired = iss.token("u-ana", ttl_s=-5)
    app = create_app(issuer=iss)
    assert run(_post(app, "/v1/chat", json={"message": "hi"},
                     headers={"Authorization": "Bearer " + expired})).status_code == 401


def test_refresh_cookie_rotation_and_reuse():
    ts = TokenService(LocalIssuer())
    _, r1 = ts.login("u-ana")
    c = ts.cookie(r1)
    assert "HttpOnly" in c and "Secure" in c and "SameSite=Strict" in c
    _, r2 = ts.rotate(r1)
    assert r2 != r1
    with pytest.raises(PermissionError):
        ts.rotate(r1)  # reuse of the old token
    with pytest.raises(PermissionError):
        ts.rotate(r2)  # whole family revoked after reuse


def test_oidc_id_token_checks():
    idp = MockOIDCProvider()
    toks = idp.authorize("u-ana", nonce="n-123")
    assert idp.identity_from(toks, "n-123")["sub"] == "u-ana"
    with pytest.raises(PermissionError):
        idp.identity_from({"access_token": toks["access_token"]}, "n-123")
    with pytest.raises(PermissionError):
        idp.identity_from(toks, "other-nonce")
    with pytest.raises(jwt.InvalidAudienceError):
        idp.verify_id_token(toks["id_token"], "n-123", client_id="someone-else")
    with pytest.raises(jwt.ExpiredSignatureError):
        idp.verify_id_token(idp.authorize("u-ana", "n", ttl_s=-5)["id_token"], "n")
    forged = MockOIDCProvider()
    forged.signer.kid = idp.signer.kid
    with pytest.raises(jwt.InvalidSignatureError):
        idp.verify_id_token(forged.authorize("u-ana", "n")["id_token"], "n")
    bad_iss = MockOIDCProvider()
    bad_iss.signer, bad_iss.iss = idp.signer, "https://evil.example"
    with pytest.raises(jwt.InvalidIssuerError):
        idp.verify_id_token(bad_iss.authorize("u-ana", "n")["id_token"], "n")
