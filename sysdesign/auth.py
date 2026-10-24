"""Slim auth layer for the store's support agent. Depth lives in Jagadish-agent-auth-security.

Classification (see docs/AUTHENTICATION.md): API keys and sessions are authentication METHODS;
Bearer is a transport PATTERN; JWT is a token FORMAT; OAuth 2 is an authorization FRAMEWORK;
OIDC adds identity via the ID token; SSO is a UX pattern on top of OIDC or SAML.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field

import jwt

from .gateway import AUDIENCE, ISSUER, LocalIssuer, verify

ACCESS_TTL_S = 15 * 60
REFRESH_TTL_S = 14 * 24 * 3600


def _h(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


class ApiKeyStore:
    """Partner webhook keys. Only the SHA-256 of a key is stored; the raw key is shown once."""

    def __init__(self, clock=time.time) -> None:
        self.rows: dict[str, dict] = {}
        self.clock = clock

    def issue(self, owner: str, scopes: list[str], ttl_s: int, tenant: str = "quillmoor") -> str:
        raw = "qmk_" + secrets.token_urlsafe(24)
        self.rows[_h(raw)] = {"owner": owner, "tenant": tenant, "scopes": scopes,
                              "expires_at": self.clock() + ttl_s, "revoked": False}
        return raw

    def verify(self, raw: str, scope: str) -> dict | None:
        rec = self.rows.get(_h(raw))
        if not rec or rec["revoked"] or self.clock() >= rec["expires_at"] or scope not in rec["scopes"]:
            return None
        return rec

    def revoke(self, raw: str) -> bool:
        rec = self.rows.get(_h(raw))
        if rec:
            rec["revoked"] = True
        return bool(rec)


class RedisSessionStore:
    """Internal-tool sessions: opaque id in a cookie, data in Redis with a TTL (survives restarts)."""

    def __init__(self, redis, ttl_s: int = 8 * 3600) -> None:
        self.r, self.ttl = redis, ttl_s

    def create(self, user: str, roles: list[str]) -> str:
        sid = secrets.token_urlsafe(32)
        self.r.set("sess:" + _h(sid), json.dumps({"user": user, "roles": roles}), ex=self.ttl)
        return sid

    def get(self, sid: str) -> dict | None:
        v = self.r.get("sess:" + _h(sid))
        return json.loads(v) if v else None

    def destroy(self, sid: str) -> None:
        self.r.delete("sess:" + _h(sid))


class MemorySessionStore:
    """Anti-pattern kept for the test: dies with the process."""

    def __init__(self) -> None:
        self.d: dict[str, dict] = {}

    def create(self, user: str, roles: list[str]) -> str:
        sid = secrets.token_urlsafe(32)
        self.d[sid] = {"user": user, "roles": roles}
        return sid

    def get(self, sid: str) -> dict | None:
        return self.d.get(sid)


class CountingKeys(dict):
    """Wraps the public-key map so tests can count lookups (local verification = no DB)."""
    lookups = 0

    def get(self, k, default=None):
        type(self).lookups += 1
        return super().get(k, default)


@dataclass
class TokenService:
    """Short-lived JWT access tokens + rotating opaque refresh tokens with reuse detection."""
    issuer: LocalIssuer
    db_lookups: int = 0
    families: dict = field(default_factory=dict)   # family -> current refresh hash
    refresh: dict = field(default_factory=dict)    # refresh hash -> {sub, family, used, exp}

    def login(self, sub: str) -> tuple[str, str]:
        fam = secrets.token_hex(8)
        return self.issuer.token(sub, ttl_s=ACCESS_TTL_S), self._new_refresh(sub, fam)

    def _new_refresh(self, sub: str, fam: str) -> str:
        raw = secrets.token_urlsafe(32)
        self.refresh[_h(raw)] = {"sub": sub, "family": fam, "used": False, "exp": time.time() + REFRESH_TTL_S}
        self.families[fam] = _h(raw)
        return raw

    def rotate(self, raw: str) -> tuple[str, str]:
        self.db_lookups += 1  # refresh is the ONE path that touches storage
        rec = self.refresh.get(_h(raw))
        if rec is None or rec["exp"] < time.time():
            raise PermissionError("invalid refresh token")
        if rec["used"] or self.families.get(rec["family"]) != _h(raw):
            self.families.pop(rec["family"], None)  # reuse => assume theft, kill the whole family
            raise PermissionError("refresh token reuse detected")
        rec["used"] = True
        return self.issuer.token(rec["sub"], ttl_s=ACCESS_TTL_S), self._new_refresh(rec["sub"], rec["family"])

    @staticmethod
    def cookie(raw: str) -> str:
        return f"qm_refresh={raw}; Path=/v1/auth/refresh; Max-Age={REFRESH_TTL_S}; HttpOnly; Secure; SameSite=Strict"

    def verify_access(self, token: str) -> dict:
        return verify(token, {self.issuer.kid: self.issuer.public_pem()})  # no storage touched


class MockOIDCProvider:
    """Local stand-in for an IdP: issues an ID token (identity) and an access token (authorization)."""

    def __init__(self, client_id: str = "qm-web") -> None:
        self.client_id = client_id
        self.signer = LocalIssuer(kid="oidc-1")
        self.iss = "https://idp.quillmoor.example"

    def authorize(self, sub: str, nonce: str, ttl_s: int = 300) -> dict:
        now = int(time.time())
        id_token = jwt.encode({"iss": self.iss, "aud": self.client_id, "sub": sub, "nonce": nonce, "iat": now,
                               "exp": now + ttl_s, "email_verified": True}, self.signer.key, algorithm="RS256",
                              headers={"kid": self.signer.kid})
        access = jwt.encode({"iss": ISSUER, "aud": AUDIENCE, "sub": sub, "scope": "chat", "iat": now,
                             "exp": now + ttl_s, "tenant": "quillmoor"}, self.signer.key, algorithm="RS256",
                            headers={"kid": self.signer.kid})
        return {"id_token": id_token, "access_token": access}

    def verify_id_token(self, token: str, expected_nonce: str, client_id: str | None = None) -> dict:
        hdr = jwt.get_unverified_header(token)
        if hdr.get("alg") != "RS256" or hdr.get("kid") != self.signer.kid:
            raise PermissionError("bad id token header")
        claims = jwt.decode(token, self.signer.public_pem(), algorithms=["RS256"], issuer=self.iss,
                            audience=client_id or self.client_id,
                            options={"require": ["exp", "iat", "sub", "iss", "aud", "nonce"]})
        if not hmac.compare_digest(claims["nonce"], expected_nonce):
            raise PermissionError("nonce mismatch")
        return claims

    def identity_from(self, tokens: dict, expected_nonce: str) -> dict:
        """Identity comes ONLY from a verified ID token; an access token is never proof of who the user is."""
        if "id_token" not in tokens:
            raise PermissionError("no id token: an access token alone is not identity")
        return self.verify_id_token(tokens["id_token"], expected_nonce)
