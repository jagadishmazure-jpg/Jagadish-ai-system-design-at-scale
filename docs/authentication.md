# Authentication (kept slim)

For depth on agent identity, delegation and token exchange, see [Jagadish-agent-auth-security](https://github.com/jagadishmazure-jpg/Jagadish-agent-auth-security). This page only sorts the terms and shows what this store uses.

## The terms are different kinds of thing
| Term | What kind of thing | One-line meaning |
|---|---|---|
| Basic | method | Username and password on every request (base64, not encryption). |
| Digest | method | Challenge-response hash of the password. Legacy. |
| API key | method | A long random secret that identifies a calling system. |
| Session | method | An opaque id in a cookie; the state lives on the server. |
| Bearer | pattern | "Whoever holds this token may use it." It says nothing about the token's format. |
| JWT | format | Signed JSON claims that can be verified without a database. |
| OAuth2 | authorization framework | Delegated access: an app gets scoped access tokens. |
| OIDC | identity layer on OAuth2 | Adds the **ID token**, which says who the user is. |
| SSO | UX pattern | Sign in once, use many apps. Built on SAML or OIDC. |

## Fit for Kestrel Goods
| Caller | Choice | Why |
|---|---|---|
| Customers | OIDC login → short-lived JWT access token (15 min) plus a rotating refresh cookie | The gateway verifies tokens locally on every request, with no DB hit. |
| Internal support tools | Server-side sessions in Redis | Instant revocation, and sessions survive an API restart. |
| Partner webhooks | Hashed API keys with scopes and expiry | Simple for machine callers, and a leaked key can be revoked. |

## What the tests prove (`tests/test_talks_auth.py`, `tests/test_talks_real_redis.py`; main's `tests/test_auth.py` covers JWT forgery, `alg=none` and unknown `kid`)
- A missing bearer token returns **401**. A webhook without an `X-API-Key` header returns **400**, and an invalid, expired or revoked key returns **401**.
- API keys are stored only as a PBKDF2-SHA256 lookup digest. They carry scopes and an expiry. A leaked key, once revoked, stops working.
- Redis sessions survive a new store instance (a real Redis in CI). Memory sessions are lost.
- JWT access tokens last 15 minutes and are verified locally (a counting key store shows zero DB lookups). An expired token returns 401. `alg=none` and unknown `kid` values are rejected.
- The refresh cookie is `HttpOnly; Secure; SameSite=Strict` and rotates on each use. Reusing an old refresh token revokes the whole family.
- The mock OIDC ID token is checked for signature, `iss`, `aud`, `exp` and `nonce`. The **access token is never treated as identity**.
