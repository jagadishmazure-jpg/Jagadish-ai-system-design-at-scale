"""FastAPI core AI service with the in-process API gateway middleware.

The same app runs as 3 stateless replicas behind nginx in docker compose
(state in Redis + Postgres) and in-process for tests (fakeredis + SQLite).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from prometheus_client import generate_latest

from .admission import Admission
from .agent import SupportAgent
from .auth import AuthError, LocalIssuer, Verifier
from .cache import SemanticCache
from .checkpoints import CheckpointStore
from .ext.authn import ApiKeyStore
from .ext.events import OrderEvents
from .jobqueue import JobQueue, QueueFull
from .observability import Metrics, make_tracer
from .providers import FakeProvider
from .resilience import ModelGateway
from .tools import Commerce

MAX_BODY = 16 * 1024
PUBLIC = {"/healthz", "/readyz", "/metrics", "/version"}
ROUTES = {"/v1/chat": "agent", "/v1/chat/stream": "agent", "/v1/refunds": "jobs", "/v1/jobs": "jobs",
          "/v1/admin/policy": "admin", "/v1/orders": "orders", "/v1/webhooks/partner": "webhooks"}
API_KEY_ROUTES = {"/v1/webhooks/partner": "webhooks:write"}  # partner callers: hashed API keys, not JWTs
_SAFE_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")
# Talk 17: every hop must pass SSE through as it is produced: no proxy buffering, no compression.
SSE_HEADERS = {"cache-control": "no-store, no-transform", "x-accel-buffering": "no"}


def build_app(redis=None, store=None, issuer: LocalIssuer | None = None, providers=None, replica: str = "r1",
              clock=None, api_keys: ApiKeyStore | None = None) -> FastAPI:
    if redis is None:
        url = os.environ.get("REDIS_URL")
        if url:  # pragma: no cover - compose path
            import redis as redis_lib

            redis = redis_lib.Redis.from_url(url)
        else:
            import fakeredis

            redis = fakeredis.FakeRedis()
    if store is None:
        dsn = os.environ.get("POSTGRES_DSN")
        store = CheckpointStore.postgres(dsn) if dsn else CheckpointStore.sqlite()
    issuer = issuer or LocalIssuer()
    verifier = Verifier({issuer.kid: issuer.public_key})
    metrics = Metrics()
    tracer, exporter = make_tracer()
    providers = providers or [FakeProvider("primary", latency_s={"small": 0.01, "large": 0.03}),
                              FakeProvider("fallback", latency_s={"small": 0.02, "large": 0.05}, seed=9)]
    admission = Admission(redis, clock=clock)
    agent = SupportAgent(admission, SemanticCache(redis, clock=clock), ModelGateway(providers),
                         metrics=metrics, tracer=tracer, clock=clock,
                         commerce=Commerce(redis))
    queue = JobQueue(redis, clock=clock)
    api_keys = api_keys or ApiKeyStore()
    order_events = OrderEvents()

    app = FastAPI(title="ai-system-design-at-scale", version="0.1.0")
    app.state.agent, app.state.queue, app.state.store = agent, queue, store
    app.state.exporter, app.state.issuer, app.state.metrics = exporter, issuer, metrics
    app.state.api_keys, app.state.order_events = api_keys, order_events

    @app.middleware("http")
    async def api_gateway(request: Request, call_next):
        rid = request.headers.get("x-request-id", "")
        rid = rid if _SAFE_ID.match(rid) else uuid.uuid4().hex  # never echo attacker-controlled ids
        request.state.request_id = rid

        path = request.url.path
        route = path if path in PUBLIC else next((p for p in ROUTES if path == p or path.startswith(p + "/")),
                                                 "unmatched")

        def reply(status: int, body: dict, headers: dict | None = None):
            metrics.http.labels(route, str(status)).inc()
            h = {"x-request-id": rid, "x-replica": replica, **(headers or {})}
            return JSONResponse(body, status_code=status, headers=h)

        if path not in PUBLIC and not any(path == p or path.startswith(p + "/") for p in ROUTES):
            return reply(404, {"error": "no route"})
        declared = request.headers.get("content-length")
        if declared is not None:
            if not declared.isdigit():
                return reply(400, {"error": "bad content-length"})
            if int(declared) > MAX_BODY:
                return reply(413, {"error": "payload too large"})
        body = await request.body()  # also enforces the limit for chunked bodies with no length header
        if len(body) > MAX_BODY:
            return reply(413, {"error": "payload too large"})
        if route in API_KEY_ROUTES:
            raw = request.headers.get("x-api-key")
            if not raw:
                return reply(400, {"error": "x-api-key header required"})
            rec = api_keys.verify(raw, API_KEY_ROUTES[route])
            if rec is None:
                return reply(401, {"error": "invalid, expired or revoked api key"})
            request.state.partner = rec
        elif path not in PUBLIC:
            auth = request.headers.get("authorization", "")
            if not auth.lower().startswith("bearer "):
                return reply(401, {"error": "missing bearer token"}, {"www-authenticate": "Bearer"})
            try:
                request.state.identity = verifier.verify(auth[7:].strip())
            except AuthError as e:
                return reply(401, {"error": str(e)}, {"www-authenticate": "Bearer"})
            if path.startswith("/v1/admin") and "policy:write" not in request.state.identity.scopes:
                return reply(403, {"error": "forbidden"})
        try:
            resp = await call_next(request)
        except Exception:
            metrics.http.labels(route, "500").inc()
            raise
        metrics.http.labels(route, str(resp.status_code)).inc()
        resp.headers["x-request-id"] = rid
        resp.headers["x-replica"] = replica
        return resp

    async def _message(request: Request) -> str | None:
        try:
            data = json.loads(await request.body() or b"{}")
        except json.JSONDecodeError:
            return None
        msg = data.get("message") if isinstance(data, dict) else None
        return msg if isinstance(msg, str) and msg.strip() else None

    def _result_response(res):
        body = {"answer": res.answer, "intent": res.intent, "rung": res.rung, "cached": res.cached,
                "degraded": res.degraded, "cost_usd": round(res.cost_usd, 8), "tokens": res.tokens}
        headers = {"retry-after": str(res.retry_after_s)} if res.status == 429 else {}
        if res.status == 429:
            body["reason"] = res.reason
        return JSONResponse(body, status_code=res.status, headers=headers)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "replica": replica}

    @app.get("/version")
    async def version():
        return {"version": os.environ.get("APP_VERSION", "dev"), "git_sha": os.environ.get("GIT_SHA", "unknown"),
                "replica": replica}

    @app.get("/readyz")
    async def readyz():
        if os.environ.get("APP_BROKEN_CONFIG") == "1":  # stands in for a bad config/secret in a release
            return JSONResponse({"ready": False, "reason": "broken_config"}, status_code=503)
        try:
            agent.admission.r.ping()
        except Exception:  # pragma: no cover - only when Redis is down
            return JSONResponse({"ready": False}, status_code=503)
        return {"ready": True}

    @app.get("/metrics")
    async def prom():
        return Response(generate_latest(metrics.registry), media_type="text/plain; version=0.0.4")

    @app.post("/v1/chat")
    async def chat(request: Request):
        msg = await _message(request)
        if msg is None:
            return JSONResponse({"error": "message required"}, status_code=422)
        res = await asyncio.to_thread(agent.handle, request.state.identity, msg)
        return _result_response(res)

    @app.post("/v1/chat/stream")
    async def chat_stream(request: Request):
        msg = await _message(request)
        if msg is None:
            return JSONResponse({"error": "message required"}, status_code=422)
        t0 = time.perf_counter()
        res = await asyncio.to_thread(agent.handle, request.state.identity, msg)
        if res.status != 200:
            return _result_response(res)

        async def events():
            first = True
            for word in res.answer.split(" "):
                if first:
                    metrics.ttft.observe(time.perf_counter() - t0)
                    first = False
                yield f"event: token\ndata: {json.dumps(word)}\n\n"
                await asyncio.sleep(0)
            yield f"event: done\ndata: {json.dumps({'intent': res.intent, 'cached': res.cached})}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers=SSE_HEADERS)

    @app.post("/v1/refunds")
    async def refunds(request: Request):
        try:
            data = json.loads(await request.body() or b"{}")
            order_id, amount = str(data["order_id"]), float(data["amount"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return JSONResponse({"error": "order_id and amount required"}, status_code=422)
        if not (amount > 0) or amount == float("inf"):
            return JSONResponse({"error": "amount must be positive"}, status_code=422)
        key = request.headers.get("idempotency-key")
        if not key or not _SAFE_ID.match(key):
            return JSONResponse({"error": "idempotency-key header required"}, status_code=422)
        ident = request.state.identity
        try:
            jid = queue.enqueue("interactive", {"kind": "refund", "user_id": ident.user_id, "order_id": order_id,
                                                "amount": amount},
                                priority="normal", idempotency_key=f"{ident.tenant}:{ident.user_id}:{key}")
        except QueueFull:
            return JSONResponse({"error": "busy"}, status_code=429, headers={"retry-after": "5"})
        return JSONResponse({"job_id": jid, "status": queue.status(jid)}, status_code=202)

    @app.get("/v1/jobs/{job_id}")
    async def job_status(job_id: str, request: Request):
        cp = store.load(job_id)
        st = queue.status(job_id)
        if st is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        owner = json.loads(queue.r.hget(f"{queue.p}:job:{job_id}", "payload"))["user_id"]
        if owner != request.state.identity.user_id:
            return JSONResponse({"error": "not found"}, status_code=404)
        return {"job_id": job_id, "status": st, "step": cp["step"] if cp else 0}

    @app.get("/v1/orders/{order_id}/events")
    async def order_events_stream(order_id: str, request: Request):
        """Push order-status changes over SSE instead of clients polling."""
        order = agent.commerce.orders.get(order_id)
        if order is None or order["user_id"] != request.state.identity.user_id:
            return JSONResponse({"error": "not found"}, status_code=404)  # same answer for missing and not-yours
        q = order_events.subscribe(order_id)

        async def stream():
            try:
                yield f"data: {json.dumps({'order_id': order_id, 'status': order['status']})}\n\n"
                while True:
                    ev = await q.get()
                    yield f"data: {json.dumps(ev)}\n\n"
                    if ev["status"] in ("delivered", "cancelled"):
                        return
            finally:
                order_events.unsubscribe(order_id, q)

        return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)

    @app.post("/v1/webhooks/partner")
    async def partner_webhook(request: Request):
        try:
            data = json.loads(await request.body() or b"{}")
            order_id, status = str(data["order_id"]), str(data["status"])
        except (json.JSONDecodeError, KeyError, TypeError):
            return JSONResponse({"error": "order_id and status required"}, status_code=422)
        if status not in {"processing", "shipped", "delivered", "cancelled"}:
            return JSONResponse({"error": "unknown status"}, status_code=422)
        order = agent.commerce.orders.get(order_id)
        if order is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        order["status"] = status
        return {"order_id": order_id, "status": status, "pushed_to": order_events.publish(order_id, status)}

    @app.post("/v1/admin/policy")
    async def policy_change(request: Request):
        try:
            data = json.loads(await request.body() or b"{}")
        except json.JSONDecodeError:
            data = None
        if not isinstance(data, dict):
            return JSONResponse({"error": "doc_id and paragraphs required"}, status_code=422)
        doc, paras = data.get("doc_id"), data.get("paragraphs")
        if not isinstance(doc, str) or not isinstance(paras, list) or not all(isinstance(p, str) for p in paras):
            return JSONResponse({"error": "doc_id and paragraphs required"}, status_code=422)
        return agent.on_policy_change(doc, paras)

    return app


def run_worker_once(queue: JobQueue, store: CheckpointStore, commerce, worker: str, pool: str = "interactive",
                    crash_after: str | None = None):
    """Claim one job and run it; used by the compose worker loop and the chaos test."""
    from .workflow import run_refund

    job = queue.claim(pool)
    if job is None:
        return None
    p = job.payload
    out = run_refund(store, commerce, job.job_id, p["user_id"], p["order_id"], p["amount"], worker,
                     crash_after=crash_after)
    queue.ack(job)
    return out
