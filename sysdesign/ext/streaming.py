"""Talk 17: streaming answers through every hop, and what to do when a stream breaks.

Pieces (all local, no vendor, no keys):
- the incident arithmetic: where 16.1 s of blank screen goes, and model speed vs reading speed;
- StreamLedger: every streamed event is written to the idempotency ledger (Redis list) under the
  request's idempotency key, so a dropped client resumes by Last-Event-ID instead of regenerating;
- FakeModelServer: a tiny asyncio HTTP server that streams SSE tokens at a set rate, can buffer the
  whole answer first (the incident), keeps generating for a short grace window after a drop, and
  stops (cancel propagation) when nobody reconnects;
- MiniProxy: a gateway stand-in that can buffer, gzip, pass through, stall, and pass a client
  disconnect upstream or not;
- sse_get: a raw-socket client that times the first token and can drop, idle-time-out and resume.

Times measured through these pieces are real wall-clock times on the box, but the model is fake:
its speed is a setting, so the times are labelled simulated-model, measured-transport.
"""
from __future__ import annotations

import asyncio
import json
import time
import zlib
from dataclasses import dataclass, field

# ------------------------------------------------------------------ incident numbers (from the story)
MEDIAN_TOKENS = 380
TOK_S = 25.0
NETWORK_S = 0.08
QUEUE_S = 0.20
PREFILL_S = 0.60
READER_TOK_S = 5.5
TURNS_PER_HOUR = 1200
# Quoted from the talk, not measured here: an A/B of a model twice as fast.
QUOTED_AB = {"model_a": {"tok_s": 25, "abandonment": 0.38, "quality_delta": 0},
             "model_b": {"tok_s": 50, "abandonment": 0.22, "quality_delta": -6},
             "source": "quoted from the talk; not measured in this repo"}


def first_token_s(network_s=NETWORK_S, queue_s=QUEUE_S, prefill_s=PREFILL_S) -> float:
    return round(network_s + queue_s + prefill_s, 3)


def breakdown(tokens: int = MEDIAN_TOKENS, tok_s: float = TOK_S) -> dict:
    gen = tokens / tok_s
    ttft = first_token_s()
    return {"network_s": NETWORK_S, "queue_s": QUEUE_S, "prefill_s": PREFILL_S,
            "generation_s": round(gen, 3), "buffered_blank_s": round(ttft + gen, 3),
            "streamed_first_token_s": ttft}


def answer_times(tok_s: float = TOK_S) -> dict:
    return {n: round(first_token_s() + n / tok_s, 2) for n in (120, MEDIAN_TOKENS, 1000)}


def reader_curve(tok_s: float = TOK_S, reader: float = READER_TOK_S, horizon_s: float = 20.0,
                 step: float = 1.0, tokens: int = MEDIAN_TOKENS) -> list[dict]:
    """Tokens on screen vs tokens a reader has read, streamed. The reader never catches the model."""
    ttft = first_token_s()
    out = []
    t = 0.0
    while t <= horizon_s + 1e-9:
        shown = 0 if t < ttft else min(tokens, int((t - ttft) * tok_s))
        read = 0 if t < ttft else min(shown, int((t - ttft) * reader))
        out.append({"t": round(t, 1), "shown": shown, "read": read})
        t += step
    return out


def model_reader_ratio(tok_s: float = TOK_S) -> float:
    return round(tok_s / READER_TOK_S, 2)


def compare_transports() -> list[dict]:
    """Why SSE for a chat answer: one request up, many events down, plain HTTP, built-in resume."""
    return [
        {"name": "long_polling", "direction": "client asks repeatedly", "plain_http": True,
         "sticky_or_broker": False, "builtin_resume": False,
         "latency": "bounded by the poll interval, not the model"},
        {"name": "websocket", "direction": "two-way", "plain_http": False,
         "sticky_or_broker": True, "builtin_resume": False,
         "latency": "per token, but the upstream half is unused for a chat answer"},
        {"name": "sse", "direction": "server to client", "plain_http": True,
         "sticky_or_broker": False, "builtin_resume": True,
         "latency": "per token; Last-Event-ID resumes after a drop"},
    ]


def choose_transport(needs_client_push: bool = False) -> str:
    return "websocket" if needs_client_push else "sse"


def poll_latency_s(interval_s: float) -> float:
    """Average extra wait a token sits on the server before a poll collects it."""
    return round(interval_s / 2, 3)


# ------------------------------------------------------------------ SSE encoding
def sse_event(event_id: int, event: str, data) -> bytes:
    return f"id: {event_id}\nevent: {event}\ndata: {json.dumps(data)}\n\n".encode()


def parse_sse(buf: bytes) -> tuple[list[dict], bytes]:
    events = []
    while b"\n\n" in buf:
        raw, buf = buf.split(b"\n\n", 1)
        ev = {}
        for line in raw.decode().splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                ev[k.strip()] = v.strip()
        if ev:
            if "id" in ev:
                ev["id"] = int(ev["id"])
            if "data" in ev:
                ev["data"] = json.loads(ev["data"])
            events.append(ev)
    return events, buf


def progress_label(ev: dict) -> str | None:
    """Turn a tool-call event into words a person can read, instead of a fake percentage bar."""
    if ev.get("event") != "tool_call":
        return None
    d = ev["data"]
    labels = {"order_status": "Checking order {order_id}", "issue_refund": "Issuing refund for order {order_id}",
              "policy_lookup": "Reading the returns policy"}
    tmpl = labels.get(d.get("tool"))
    if tmpl is None:
        return "Working on it"
    try:
        return tmpl.format(**d.get("args", {}))
    except KeyError:
        return "Working on it"


# ------------------------------------------------------------------ ledger
class StreamLedger:
    """Streamed events kept under the request's idempotency key, next to the job ledger.

    Keys: {p}:stream:{key}:events (LIST of encoded events), {p}:stream:{key}:meta (HASH state,
    expires_at). Expiry is checked against an injected clock so tests do not sleep.
    """

    def __init__(self, redis, clock=None, ttl_s: float = 300.0, prefix: str = "sd") -> None:
        self.r = redis
        self.clock = clock
        self.ttl_s = ttl_s
        self.p = prefix

    def _now(self) -> float:
        return self.clock.now() if self.clock else time.time()

    def _k(self, key: str, part: str) -> str:
        return f"{self.p}:stream:{key}:{part}"

    def open(self, key: str) -> bool:
        """True if this call created the stream (first click); False if it already exists."""
        created = self.r.hsetnx(self._k(key, "meta"), "state", "inflight")
        self.r.hset(self._k(key, "meta"), "expires_at", self._now() + self.ttl_s)
        return bool(created)

    def append(self, key: str, event_id: int, event: str, data) -> None:
        self.r.rpush(self._k(key, "events"), json.dumps([event_id, event, data]))

    def finish(self, key: str, state: str = "done") -> None:
        self.r.hset(self._k(key, "meta"), "state", state)

    def state(self, key: str) -> str | None:
        if self.expired(key):
            return None
        v = self.r.hget(self._k(key, "meta"), "state")
        return v.decode() if isinstance(v, bytes) else v

    def expired(self, key: str) -> bool:
        v = self.r.hget(self._k(key, "meta"), "expires_at")
        return v is None or float(v) < self._now()

    def after(self, key: str, last_id: int) -> list[tuple[int, str, object]]:
        if self.expired(key):
            return []
        rows = [json.loads(x) for x in self.r.lrange(self._k(key, "events"), 0, -1)]
        return [tuple(r) for r in rows if r[0] > last_id]

    def count(self, key: str) -> int:
        return self.r.llen(self._k(key, "events"))


def resume_plan(ledger: StreamLedger, key: str, last_id: int) -> dict:
    """What to send a client that reconnects with Last-Event-ID."""
    if ledger.expired(key):
        return {"action": "truncated", "events": [
            (last_id + 1, "truncated", {"after_event": last_id,
                                        "message": "The rest of this answer is no longer available.",
                                        "offer": "continue"})]}
    return {"action": "resume", "events": ledger.after(key, last_id)}


# ------------------------------------------------------------------ fake model server
@dataclass
class ServerStats:
    generated: dict = field(default_factory=dict)   # key -> tokens produced
    cancelled: dict = field(default_factory=dict)   # key -> True when generation was stopped
    starts: dict = field(default_factory=dict)      # key -> number of generations started


TOOL_EVENTS = [("order_status", {"order_id": "9917"}), ("issue_refund", {"order_id": "9917"})]


class FakeModelServer:
    """Streams `tokens` SSE token events after `ttft_s`, at `tok_s`, under an idempotency key.

    buffered=True holds the whole answer and sends it at the end (the incident's service setting).
    A reconnect with the same key and Last-Event-ID reads from the ledger; generation is not restarted.
    regenerate_on_reconnect=True is the wrong fix, kept to count its cost.
    After every client leaves, generation keeps going for `grace_s`, then stops (cancel propagation)
    unless propagate_cancel=False.
    """

    def __init__(self, ledger: StreamLedger, ttft_s=0.88, tok_s=TOK_S, tokens=MEDIAN_TOKENS,
                 buffered=False, grace_s=1.0, propagate_cancel=True, regenerate_on_reconnect=False,
                 accel_header=True):
        self.ledger = ledger
        self.ttft_s, self.tok_s, self.tokens = ttft_s, tok_s, tokens
        self.buffered = buffered
        self.grace_s = grace_s
        self.propagate_cancel = propagate_cancel
        self.regenerate = regenerate_on_reconnect
        self.accel_header = accel_header  # the incident's service did not send x-accel-buffering: no
        self.stats = ServerStats()
        self._tasks: dict[str, asyncio.Task] = {}
        self._readers: dict[str, int] = {}
        self._wake: dict[str, asyncio.Event] = {}
        self._left_at: dict[str, float] = {}
        self.server = None
        self.port = None

    async def start(self, host="127.0.0.1", port=0):
        self.server = await asyncio.start_server(self._handle, host, port)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def close(self):
        for t in self._tasks.values():
            t.cancel()
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    async def _generate(self, key: str):
        self.stats.starts[key] = self.stats.starts.get(key, 0) + 1
        loop = asyncio.get_running_loop()
        await asyncio.sleep(self.ttft_s * 0.25)
        eid = 0
        for tool, args in TOOL_EVENTS:
            eid += 1
            self.ledger.append(key, eid, "tool_call", {"tool": tool, "args": args})
            self._wake[key].set()
        await asyncio.sleep(self.ttft_s * 0.75)
        t0 = loop.time()
        for i in range(self.tokens):
            target = t0 + i / self.tok_s
            delay = target - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            if self.propagate_cancel and self._readers.get(key, 0) == 0:
                left = self._left_at.get(key)
                if left is not None and loop.time() - left >= self.grace_s:
                    self.stats.cancelled[key] = True
                    self.ledger.finish(key, "cancelled")
                    self._wake[key].set()
                    return
            eid += 1
            self.ledger.append(key, eid, "token", f"w{i}")
            self.stats.generated[key] = self.stats.generated.get(key, 0) + 1
            self._wake[key].set()
        eid += 1
        self.ledger.append(key, eid, "done", {"tokens": self.tokens})
        self.ledger.finish(key)
        self._wake[key].set()

    async def _handle(self, reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, ConnectionError):
            writer.close()
            return
        lines = head.decode().split("\r\n")
        path = lines[0].split(" ")[1]
        hdr = {k.lower(): v.strip() for k, v in (ln.split(":", 1) for ln in lines[1:] if ":" in ln)}
        key = path.rsplit("/", 1)[-1]
        last_id = int(hdr.get("last-event-id", "0") or 0)
        self._wake.setdefault(key, asyncio.Event())
        if self.regenerate and key in self._tasks:
            self._tasks[key].cancel()
            self.ledger.r.delete(self.ledger._k(key, "events"))
            self.ledger.r.delete(self.ledger._k(key, "meta"))
            self._tasks.pop(key)
            last_id = 0
        created = self.ledger.open(key) if key not in self._tasks else False
        plan = None
        if key not in self._tasks and not created:
            plan = resume_plan(self.ledger, key, last_id)  # reconnect after the server lost the task
        elif key not in self._tasks:
            self._tasks[key] = asyncio.create_task(self._generate(key))
        writer.write(b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\ncache-control: no-store\r\n"
                     + (b"x-accel-buffering: no\r\n" if self.accel_header else b"")
                     + b"connection: close\r\n\r\n")
        self._readers[key] = self._readers.get(key, 0) + 1
        self._left_at.pop(key, None)
        try:
            if plan is not None:
                for e in plan["events"]:
                    writer.write(sse_event(*e))
                await writer.drain()
                return
            sent = last_id
            held = b""
            while True:
                for e in self.ledger.after(key, sent):
                    chunk = sse_event(*e)
                    sent = e[0]
                    if self.buffered:
                        held += chunk
                    else:
                        writer.write(chunk)
                if not self.buffered:
                    await writer.drain()
                if self.ledger.state(key) in ("done", "cancelled"):
                    if self.ledger.after(key, sent):
                        continue
                    break
                if reader.at_eof():
                    raise ConnectionResetError
                self._wake[key].clear()
                try:
                    await asyncio.wait_for(self._wake[key].wait(), 0.05)
                except asyncio.TimeoutError:
                    pass  # no new event yet; loop and re-check the socket and ledger
            if held:
                writer.write(held)
                await writer.drain()
        except (ConnectionError, OSError):
            pass  # client went away; the finally block records it and cleans up
        finally:
            self._readers[key] -= 1
            if self._readers[key] == 0:
                self._left_at[key] = asyncio.get_running_loop().time()
            try:
                writer.close()
            except Exception:  # noqa: BLE001 - socket already gone
                pass


# ------------------------------------------------------------------ mini gateway
class MiniProxy:
    """mode: 'pass' (forward each read), 'buffer' (hold the whole body), 'gzip' (compress the stream
    with a normal deflate buffer, the way a gateway with gzip on would). stall_after: stop forwarding
    after N bytes of body, keep the client socket open (a stalled hop). propagate_cancel: close the
    upstream socket when the client leaves; if False, keep draining upstream (tokens into the void)."""

    def __init__(self, upstream_port: int, mode: str = "pass", propagate_cancel: bool = True,
                 stall_after: int | None = None):
        assert mode in ("pass", "buffer", "gzip")
        self.up = upstream_port
        self.mode = mode
        self.propagate_cancel = propagate_cancel
        self.stall_after = stall_after
        self.server = None
        self.port = None
        self._stall_used = False
        self._bg: set = set()

    async def start(self, host="127.0.0.1", port=0):
        self.server = await asyncio.start_server(self._handle, host, port)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def close(self):
        for t in list(self._bg):
            t.cancel()
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, creader, cwriter):
        try:
            head = await creader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, ConnectionError):
            cwriter.close()
            return
        ureader, uwriter = await asyncio.open_connection("127.0.0.1", self.up)
        uwriter.write(head)
        await uwriter.drain()
        resp_head = await ureader.readuntil(b"\r\n\r\n")
        if self.mode == "gzip":
            resp_head = resp_head[:-2] + b"content-encoding: gzip\r\n\r\n"
        comp = zlib.compressobj(6, zlib.DEFLATED, 31) if self.mode == "gzip" else None
        stall = self.stall_after if not self._stall_used else None
        if stall is not None:
            self._stall_used = True
        held = b""
        sent_body = 0
        client_gone = False

        async def watch_client():
            await creader.read()  # EOF when the client leaves
            return True

        watcher = asyncio.create_task(watch_client())
        try:
            if self.mode != "buffer":
                cwriter.write(resp_head)
                await cwriter.drain()
            while True:
                rd = asyncio.create_task(ureader.read(65536))
                done, _ = await asyncio.wait({rd, watcher}, return_when=asyncio.FIRST_COMPLETED)
                if watcher in done and not client_gone:
                    client_gone = True
                    if self.propagate_cancel:
                        rd.cancel()
                        break
                chunk = await rd
                if not chunk:
                    break
                if client_gone:
                    continue  # draining upstream for nobody
                if self.mode == "buffer":
                    held += chunk
                    continue
                if self.mode == "gzip":
                    chunk = comp.compress(chunk)
                if stall is not None and sent_body + len(chunk) > stall:
                    chunk = chunk[: max(0, stall - sent_body)]
                    if chunk:
                        cwriter.write(chunk)
                        await cwriter.drain()
                    await watcher  # hold the client open, send nothing more
                    client_gone = True
                    if self.propagate_cancel:
                        break
                    continue
                if chunk:
                    sent_body += len(chunk)
                    cwriter.write(chunk)
                    await cwriter.drain()
            if not client_gone:
                if self.mode == "buffer":
                    cwriter.write(resp_head + held)
                elif self.mode == "gzip":
                    cwriter.write(comp.flush())
                await cwriter.drain()
        except (ConnectionError, OSError):
            pass  # client went away; the finally block records it and cleans up
        finally:
            watcher.cancel()
            uwriter.close()
            try:
                cwriter.close()
            except Exception:  # noqa: BLE001
                pass


# ------------------------------------------------------------------ client
@dataclass
class StreamResult:
    first_token_s: float | None = None
    first_event_s: float | None = None
    done_s: float | None = None
    tokens: int = 0
    last_id: int = 0
    events: list = field(default_factory=list)
    ended: str = "eof"      # eof | done | dropped | idle_timeout


def _dechunk(buf: bytes) -> tuple[bytes, bytes]:
    out = b""
    while True:
        if b"\r\n" not in buf:
            return out, buf
        size_line, rest = buf.split(b"\r\n", 1)
        size = int(size_line.split(b";")[0] or b"0", 16)
        if size == 0:
            return out, b""
        if len(rest) < size + 2:
            return out, buf
        out += rest[:size]
        buf = rest[size + 2:]


async def sse_get(port: int, key: str, last_id: int = 0, drop_after_tokens: int | None = None,
                  idle_timeout_s: float | None = None, t0: float | None = None,
                  total_timeout_s: float = 120.0) -> StreamResult:
    loop = asyncio.get_running_loop()
    t0 = loop.time() if t0 is None else t0
    res = StreamResult(last_id=last_id)
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    hdr = f"GET /v1/stream/{key} HTTP/1.1\r\nhost: local\r\naccept: text/event-stream\r\naccept-encoding: gzip\r\n"
    if last_id:
        hdr += f"last-event-id: {last_id}\r\n"
    writer.write((hdr + "\r\n").encode())
    await writer.drain()
    raw = b""
    head = None
    chunked = gz = False
    dec = None
    pending = b""
    body = b""
    try:
        while True:
            timeout = idle_timeout_s if (idle_timeout_s and head is not None) else total_timeout_s
            try:
                data = await asyncio.wait_for(reader.read(65536), timeout)
            except asyncio.TimeoutError:
                res.ended = "idle_timeout"
                break
            if not data:
                break
            if head is None:
                raw += data
                if b"\r\n\r\n" not in raw:
                    continue
                head, data = raw.split(b"\r\n\r\n", 1)
                h = head.decode().lower()
                chunked = "transfer-encoding: chunked" in h
                gz = "content-encoding: gzip" in h
                dec = zlib.decompressobj(31) if gz else None
            if chunked:
                plain, pending = _dechunk(pending + data)
            else:
                plain = data
            if gz:
                plain = dec.decompress(plain)
            body += plain
            evs, body = parse_sse(body)
            now = loop.time() - t0
            for ev in evs:
                res.events.append(ev)
                res.last_id = ev.get("id", res.last_id)
                if res.first_event_s is None:
                    res.first_event_s = round(now, 4)
                if ev.get("event") == "token":
                    res.tokens += 1
                    if res.first_token_s is None:
                        res.first_token_s = round(now, 4)
                if ev.get("event") in ("done", "truncated"):
                    res.done_s = round(now, 4)
                    res.ended = "done" if ev["event"] == "done" else "truncated"
            if res.ended in ("done", "truncated"):
                break
            if drop_after_tokens is not None and res.tokens >= drop_after_tokens:
                res.ended = "dropped"
                break
    finally:
        writer.close()
    return res


async def resilient_get(port: int, key: str, idle_timeout_s: float, max_reconnects: int = 3) -> dict:
    """Client that reconnects with Last-Event-ID after a drop or a stall."""
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    last, tokens, attempts, endings, first = 0, 0, 0, [], None
    while attempts <= max_reconnects:
        r = await sse_get(port, key, last_id=last, idle_timeout_s=idle_timeout_s, t0=t0)
        attempts += 1
        endings.append(r.ended)
        tokens += r.tokens
        last = r.last_id
        first = first if first is not None else r.first_token_s
        if r.ended in ("done", "truncated"):
            break
    return {"tokens": tokens, "connections": attempts, "endings": endings, "first_token_s": first,
            "total_s": round(loop.time() - t0, 3), "last_id": last}


def results() -> dict:
    """Deterministic talk 17 arithmetic (no sockets); the measured replay is scripts/stream_replay.py."""
    return {"note": "arithmetic from the story's settings; abandonment and quality are quoted",
            "breakdown": breakdown(), "answer_times_s": answer_times(),
            "model_over_reader": model_reader_ratio(), "faster_model_over_reader": model_reader_ratio(50),
            "reader_curve": reader_curve(), "quoted_ab": QUOTED_AB, "transports": compare_transports(),
            "turns_per_hour": TURNS_PER_HOUR}
