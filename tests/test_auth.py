import time

import jwt
import pytest

from sysdesign.auth import AUDIENCE, ISSUER, AuthError, LocalIssuer, Verifier


def v(issuer):
    return Verifier({issuer.kid: issuer.public_key})


def test_valid_token_yields_identity(issuer):
    ident = v(issuer).verify(issuer.mint("u-ava", tenant="retail", scopes=("chat", "policy:write")))
    assert ident.user_id == "u-ava" and ident.tenant == "retail" and "policy:write" in ident.scopes


def test_expired_token_rejected(issuer):
    tok = issuer.mint("u-ava", ttl=1, now=time.time() - 100)
    with pytest.raises(AuthError):
        v(issuer).verify(tok)


def test_token_from_other_key_rejected(issuer):
    other = LocalIssuer(kid=issuer.kid)  # same kid, different key
    with pytest.raises(AuthError):
        v(issuer).verify(other.mint("u-ava"))


def test_unknown_kid_rejected(issuer):
    with pytest.raises(AuthError):
        v(issuer).verify(LocalIssuer(kid="rogue").mint("u-ava"))


def test_alg_none_rejected(issuer):
    tok = jwt.encode({"iss": ISSUER, "aud": AUDIENCE, "sub": "x", "tenant": "t", "iat": 1, "exp": 2**31},
                     None, algorithm="none", headers={"kid": issuer.kid})
    with pytest.raises(AuthError):
        v(issuer).verify(tok)


def test_wrong_audience_rejected(issuer):
    with pytest.raises(AuthError):
        v(issuer).verify(issuer.mint("u-ava", aud="other-app"))


def test_missing_tenant_rejected(issuer):
    with pytest.raises(AuthError):
        v(issuer).verify(issuer.mint("u-ava", tenant=""))


def test_garbage_token_rejected(issuer):
    with pytest.raises(AuthError):
        v(issuer).verify("not.a.jwt")


def test_shared_pem_key_lets_any_replica_verify(tmp_path, issuer):
    p = tmp_path / "k.pem"
    p.write_bytes(issuer.private_pem())
    a, b = LocalIssuer.from_pem_file(str(p)), LocalIssuer.from_pem_file(str(p))
    assert Verifier({b.kid: b.public_key}).verify(a.mint("u-ava")).user_id == "u-ava"
