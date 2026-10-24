"""In-process API gateway (inbound traffic). In docker compose nginx sits in front and does
TLS-free routing, body-size limits and request ids too; this middleware repeats the checks so a
request that skips nginx (or the CI run, which has no nginx) gets the same treatment.

Checks, in order: request id -> route allowlist -> body size (413) -> JWT (401/403) -> app.
JWTs are OIDC-style RS256 tokens signed by a local dev issuer (no external IdP, no keys in git).
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

ISSUER = "https://auth.quillmoor.example"
AUDIENCE = "support-agent"


@dataclass
class LocalIssuer:
    """Dev-only OIDC-style issuer: generates an RSA key in memory and signs tokens."""
    kid: str = "dev-1"
    key: rsa.RSAPrivateKey = field(default_factory=lambda: rsa.generate_private_key(65537, 2048))

    def public_pem(self) -> bytes:
        return self.key.public_key().public_bytes(serialization.Encoding.PEM,
                                                  serialization.PublicFormat.SubjectPublicKeyInfo)

    def token(self, sub: str, tenant: str = "quillmoor", scopes: str = "chat orders:read",
              ttl_s: int = 3600, **extra) -> str:
        now = int(time.time())
        claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": sub, "tenant": tenant, "scope": scopes,
                  "iat": now, "exp": now + ttl_s, **extra}
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": self.kid})


class AuthError(Exception):
    def __init__(self, status: int, msg: str) -> None:
        super().__init__(msg)
        self.status = status


def verify(token: str, public_keys: dict[str, bytes]) -> dict:
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as e:
        raise AuthError(401, "malformed token") from e
    if header.get("alg") != "RS256":
        raise AuthError(401, "unsupported alg")  # blocks alg=none and HS256-with-public-key confusion
    key = public_keys.get(header.get("kid", ""))
    if key is None:
        raise AuthError(401, "unknown key id")
    try:
        claims = jwt.decode(token, key, algorithms=["RS256"], audience=AUDIENCE, issuer=ISSUER,
                            options={"require": ["exp", "iat", "sub", "iss", "aud"]})
    except jwt.PyJWTError as e:
        raise AuthError(401, f"invalid token: {type(e).__name__}") from e
    if not claims.get("tenant"):
        raise AuthError(403, "no tenant claim")
    return claims


ROUTES = {  # path prefix -> (service name, required scope or None for public)
    "/healthz": ("core", None),
    "/metrics": ("core", None),
    "/readyz": ("core", None),
    "/version": ("core", None),
    "/v1/orders": ("core", "orders:read"),
    "/v1/chat": ("core", "chat"),
    "/v1/jobs": ("core", "chat"),
    "/v1/actions": ("core", "chat"),
    "/admin/": ("admin", "admin"),
    "/v1/webhooks/partner": ("core", "apikey:webhooks:write"),  # partners authenticate with an API key
}


def route_for(path: str):
    for prefix, target in ROUTES.items():
        base = prefix.rstrip("/")
        if path == base or path.startswith(base + "/"):
            return target
    return None


class GatewayMiddleware:
    """Pure ASGI middleware so it can read Content-Length and stream-count the body."""

    def __init__(self, app, public_keys: dict[str, bytes], max_body_bytes: int = 16_384, api_keys=None) -> None:
        self.app = app
        self.api_keys = api_keys  # sysdesign.auth.ApiKeyStore (hash-only)
        self.public_keys = public_keys
        self.max_body = max_body_bytes

    async def _reject(self, send, status: int, msg: str, rid: str) -> None:
        body = json.dumps({"error": msg, "request_id": rid}).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), (b"x-request-id", rid.encode())]})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        rid = headers.get("x-request-id", "")
        if not rid or len(rid) > 64 or not rid.replace("-", "").isalnum():
            rid = uuid.uuid4().hex  # never trust or reflect an arbitrary client value
        target = route_for(scope["path"])
        if target is None:
            return await self._reject(send, 404, "no route", rid)
        service, need = target
        try:
            declared = int(headers.get("content-length", "0") or 0)
        except ValueError:
            return await self._reject(send, 400, "bad content-length", rid)
        if declared > self.max_body:
            return await self._reject(send, 413, "payload too large", rid)

        # Enforce the limit on the bytes actually received too (chunked bodies have no length).
        chunks, total = [], 0
        while True:
            msg = await receive()
            if msg["type"] == "http.request":
                total += len(msg.get("body", b""))
                if total > self.max_body:
                    return await self._reject(send, 413, "payload too large", rid)
                chunks.append(msg)
                if not msg.get("more_body"):
                    break
            else:
                chunks.append(msg)
                break

        identity = None
        if need is not None and need.startswith("apikey:"):
            raw = headers.get("x-api-key")
            if not raw:
                return await self._reject(send, 400, "missing X-API-Key header", rid)
            rec = self.api_keys.verify(raw, need[len("apikey:"):]) if self.api_keys else None
            if rec is None:
                return await self._reject(send, 401, "invalid, expired or revoked api key", rid)
            identity = {"sub": rec["owner"], "tenant": rec["tenant"], "scope": " ".join(rec["scopes"]),
                        "auth": "api_key"}
        elif need is not None:
            auth = headers.get("authorization", "")
            if not auth.lower().startswith("bearer "):
                return await self._reject(send, 401, "missing bearer token", rid)
            try:
                identity = verify(auth[7:], self.public_keys)
            except AuthError as e:
                return await self._reject(send, e.status, str(e), rid)
            if need not in identity.get("scope", "").split():
                return await self._reject(send, 403, "missing scope", rid)

        scope = dict(scope)
        scope["state"] = {**scope.get("state", {}), "request_id": rid, "identity": identity, "service": service}

        async def replay():
            # Replay the buffered body, then defer to the real channel so a streaming response only
            # sees a disconnect when the client actually goes away.
            return chunks.pop(0) if chunks else await receive()

        async def send_with_id(message):
            if message["type"] == "http.response.start":
                message = dict(message)
                message["headers"] = list(message.get("headers", [])) + [(b"x-request-id", rid.encode())]
            await send(message)

        return await self.app(scope, replay, send_with_id)
