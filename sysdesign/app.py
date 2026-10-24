"""FastAPI core service. Stateless: all shared state lives in Redis and Postgres (or their
in-process stand-ins), so any replica can serve any request and resume any workflow."""
from __future__ import annotations

import json
import os
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.middleware.gzip import GZipMiddleware

from .build import System, build
from .gateway import GatewayMiddleware, LocalIssuer
from .guardrails import check_input, check_output
from .pii import redact
from .tools import ORDERS


def create_app(system: System | None = None, issuer: LocalIssuer | None = None, replica: str = "api-1",
               max_body_bytes: int = 16_384, api_keys=None):
    system = system or _system_from_env()
    issuer = issuer or LocalIssuer()
    app = FastAPI(title="support-agent core service")
    app.state.system, app.state.issuer, app.state.replica = system, issuer, replica
    app.state.healthy = True

    def ident(request: Request) -> dict:
        return request.scope["state"]["identity"]

    def rid(request: Request) -> str:
        return request.scope["state"]["request_id"]

    def reject(decision, request):
        system.metrics.rejections.labels(decision.reason).inc()
        return JSONResponse({"error": "too many requests", "reason": decision.reason, "request_id": rid(request)},
                            status_code=429 if decision.reason != "invalid_estimate" else 400,
                            headers={"Retry-After": str(decision.retry_after_s)})

    async def body(request: Request) -> dict:
        try:
            data = json.loads(await request.body() or b"{}")
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    @app.middleware("http")
    async def count_requests(request: Request, call_next):
        try:
            resp = await call_next(request)
        except Exception:
            system.metrics.requests.labels(request.url.path[:40], "500").inc()
            raise
        path = request.url.path if request.url.path.count("/") <= 3 else "/".join(request.url.path.split("/")[:3])
        system.metrics.requests.labels(path, str(resp.status_code)).inc()
        return resp

    @app.get("/healthz")
    async def healthz():
        if not app.state.healthy:
            return JSONResponse({"status": "down", "replica": replica}, status_code=503)
        return {"status": "ok", "replica": replica}

    @app.get("/readyz")
    async def readyz():
        # readiness = can this replica serve traffic right now (deps + config). A release whose
        # config is broken fails here, so Kubernetes never routes users to it.
        if os.environ.get("APP_BROKEN_CONFIG") == "1" or not app.state.healthy:
            return JSONResponse({"ready": False, "reason": "config check failed"}, status_code=503)
        return {"ready": True}

    @app.get("/version")
    async def version():
        return {"version": os.environ.get("APP_VERSION", "dev"), "git_sha": os.environ.get("GIT_SHA", "unknown"),
                "replica": replica}

    @app.get("/v1/orders/{order_id}/events")
    async def order_events(order_id: str, request: Request):
        """Live order-status push over SSE: the server sends a line when the status changes, so the
        client holds one idle connection instead of polling every few seconds."""
        claims = ident(request)
        order = ORDERS.get(order_id)
        if not order or order["owner"] != claims["sub"]:
            return JSONResponse({"error": "not found"}, status_code=404)
        q = system.order_events.subscribe(order_id)

        async def gen():
            try:
                yield f"data: {json.dumps({'order_id': order_id, 'status': order['status']})}\n\n"
                while True:
                    ev = await q.get()
                    yield f"data: {json.dumps(ev)}\n\n"
                    if ev["status"] in ("delivered", "cancelled"):
                        return
            finally:
                system.order_events.unsubscribe(order_id, q)
        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/metrics")
    async def metrics():
        return Response(system.metrics.expose(), media_type="text/plain; version=0.0.4")

    @app.post("/v1/chat")
    async def chat(request: Request):
        data = await body(request)
        text = str(data.get("message", ""))[:4000]
        if not text.strip():
            return JSONResponse({"error": "message required"}, status_code=422)
        me = ident(request)
        d = system.admission.admit(me["tenant"], me["sub"], system.agent.estimate_tokens(text))
        if not d.allowed:
            return reject(d, request)
        t0 = time.perf_counter()
        out = {"tokens": 0}
        try:
            out = await system.agent.handle(text, me, str(data.get("conversation_id", me["sub"]))[:64])
        finally:
            system.admission.release(d.reservation, out["tokens"])  # always free the slot
        return {"answer": out["answer"], "intent": out["intent"], "tier": out["tier"], "proposal": out["proposal"],
                "run_id": out["run_id"], "replica": replica, "request_id": rid(request),
                "latency_s": round(time.perf_counter() - t0, 4)}

    @app.post("/v1/chat/stream")
    async def chat_stream(request: Request):
        """SSE. Only knowledge answers stream from the model; output guardrails run per chunk window."""
        data = await body(request)
        text = str(data.get("message", ""))[:4000]
        me = ident(request)
        if check_input(text).blocked:
            return JSONResponse({"error": "blocked"}, status_code=400)
        d = system.admission.admit(me["tenant"], me["sub"], system.agent.estimate_tokens(text))
        if not d.allowed:
            return reject(d, request)
        hits = [c for c in system.kb.search(text) if c["score"] >= 0.20]
        prompt = "CONTEXT: " + " ".join(c["text"] for c in hits) + "\nQUESTION: " + redact(text)
        provider = system.gateway.providers["small"]

        async def events():
            t0 = time.perf_counter()
            first, n = None, 0
            try:
                if not hits:
                    yield "data: " + json.dumps({"token": "I couldn't find that in our policies."}) + "\n\n"
                else:
                    async for w in provider.stream(prompt):
                        if first is None:
                            first = time.perf_counter() - t0
                            system.metrics.ttft.observe(first)
                        n += 1
                        yield "data: " + json.dumps({"token": check_output(w).text}) + "\n\n"
                yield "event: done\ndata: " + json.dumps({"ttft_s": round(first or 0, 4), "tokens": n}) + "\n\n"
            finally:
                system.admission.release(d.reservation, n)

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.post("/v1/actions/confirm")
    async def confirm(request: Request):
        data = await body(request)
        out = await system.gate.confirm(str(data.get("proposal_id", "")), str(data.get("confirm_digest", "")),
                                        ident(request))
        return out

    @app.post("/v1/jobs")
    async def submit_job(request: Request):
        from .jobqueue import QueueFull
        data = await body(request)
        key = request.headers.get("idempotency-key", "")
        if not key or len(key) > 64:
            return JSONResponse({"error": "Idempotency-Key header required"}, status_code=400)
        me = ident(request)
        try:
            jid, created = app.state.queue.enqueue(data.get("pool", "reports") if data.get("pool") in
                                                   app.state.queue.pools else "reports",
                                                   {"user": me["sub"], "kind": str(data.get("kind", "report"))[:32]},
                                                   f"{me['sub']}:{key}", "normal")
        except QueueFull:
            return JSONResponse({"error": "queue full"}, status_code=429, headers={"Retry-After": "5"})
        return JSONResponse({"job_id": jid, "created": created}, status_code=202)

    @app.get("/v1/jobs/{job_id}")
    async def job_status(job_id: str):
        return {"job_id": job_id, "status": app.state.queue.status(job_id)}

    @app.post("/admin/kb/changes")
    async def kb_changes(request: Request):
        data = await body(request)
        changed = system.kb.apply_changes(list(data.get("events", []))[:100])
        dropped = system.cache.invalidate_chunks(changed)
        return {"reindexed": changed, "cache_entries_dropped": dropped}

    @app.post("/v1/webhooks/partner")
    async def partner_webhook(request: Request):
        data = await body(request)
        oid, status = str(data.get("order_id", "")), str(data.get("status", ""))
        if oid not in ORDERS or status not in ("packed", "in_transit", "delivered", "cancelled"):
            return JSONResponse({"error": "bad event"}, status_code=422)
        ORDERS[oid]["status"] = status
        return {"accepted": True, "pushed_to": system.order_events.publish(oid, status)}

    from .auth import ApiKeyStore
    app.state.api_keys = api_keys or ApiKeyStore()

    from .jobqueue import JobQueue, PoolConfig
    app.state.queue = JobQueue(system.redis, {"refunds": PoolConfig(max_backlog=500),
                                              "reports": PoolConfig(max_backlog=200)})
    keys = {issuer.kid: issuer.public_pem()}
    wrapped = GatewayMiddleware(GZipMiddleware(app, minimum_size=1024), keys, max_body_bytes, api_keys=app.state.api_keys)
    wrapped.fastapi = app
    return wrapped


def _system_from_env() -> System:
    url = os.environ.get("REDIS_URL")
    redis = None
    if url:
        import redis as redis_lib
        redis = redis_lib.Redis.from_url(url)
    return build(redis=redis, dsn=os.environ.get("DATABASE_URL", ":memory:"))
