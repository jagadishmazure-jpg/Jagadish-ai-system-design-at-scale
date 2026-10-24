"""OIDC-style JWT verification with locally generated RS256 keys (no identity provider needed).

A real deployment swaps LocalIssuer for the provider's JWKS endpoint; the
verifier checks signature, issuer, audience, expiry and the required claims.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

ISSUER = "https://id.kestrel-goods.example"
AUDIENCE = "support-agent"


@dataclass(frozen=True)
class Identity:
    user_id: str
    tenant: str
    tier: str
    scopes: frozenset[str]


class AuthError(Exception):
    pass


class LocalIssuer:
    def __init__(self, kid: str = "dev-1", private_key=None) -> None:
        self.kid = kid
        self._key = private_key or rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.public_key = self._key.public_key()

    @classmethod
    def from_pem_file(cls, path: str, kid: str = "dev-1") -> LocalIssuer:
        """Load a shared dev key so every replica verifies the same tokens (compose)."""
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        with open(path, "rb") as fh:
            return cls(kid, load_pem_private_key(fh.read(), password=None))

    def private_pem(self) -> bytes:
        from cryptography.hazmat.primitives import serialization

        return self._key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption())

    def mint(self, sub: str, tenant: str = "retail", tier: str = "standard", scopes=("chat",),
             ttl: int = 600, now: float | None = None, **extra) -> str:
        now = int(now if now is not None else time.time())
        claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": sub, "tenant": tenant, "tier": tier,
                  "scope": " ".join(scopes), "iat": now, "exp": now + ttl, **extra}
        return jwt.encode(claims, self._key, algorithm="RS256", headers={"kid": self.kid})


class Verifier:
    def __init__(self, keys: dict) -> None:
        self.keys = keys  # kid -> public key

    def verify(self, token: str, now: float | None = None) -> Identity:
        try:
            kid = jwt.get_unverified_header(token).get("kid")
        except jwt.PyJWTError as e:
            raise AuthError("malformed token") from e
        key = self.keys.get(kid)
        if key is None:
            raise AuthError("unknown signing key")
        try:
            # algorithms pinned: rejects alg=none and HS256-with-public-key confusion
            claims = jwt.decode(token, key, algorithms=["RS256"], audience=AUDIENCE, issuer=ISSUER,
                                options={"require": ["exp", "iat", "sub", "iss", "aud"], "verify_exp": now is None},
                                leeway=5)
        except jwt.PyJWTError as e:
            raise AuthError("invalid token") from e
        if now is not None and claims["exp"] < now:
            raise AuthError("invalid token")
        tenant = claims.get("tenant")
        if not tenant or not isinstance(tenant, str):
            raise AuthError("missing tenant")
        return Identity(user_id=claims["sub"], tenant=tenant, tier=str(claims.get("tier", "standard")),
                        scopes=frozenset(str(claims.get("scope", "")).split()))
