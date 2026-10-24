"""Talk 17 replay: buffered vs streamed time to first token through real local sockets.

Writes docs/results/stream_replay.json. Times are wall-clock measurements on this machine; the model
is a fake that emits tokens at a set rate (ttft 0.88 s, 25 tok/s, 380 tokens at --scale 1), so
label them "measured transport, simulated model". If an nginx binary is on PATH, the same server is
also measured behind real nginx with buffering on, off, gzip on the stream, and this repo's fixed
gzip rule. No network beyond 127.0.0.1, no keys.

  python scripts/stream_replay.py            # full talk speed, about 3 minutes
  python scripts/stream_replay.py --scale 20 # 20x faster, for a quick look
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import platform
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import fakeredis  # noqa: E402

from sysdesign.clock import FakeClock  # noqa: E402
from sysdesign.ext import streaming as st  # noqa: E402

OUT = ROOT / "docs/results/stream_replay.json"

NGINX_VARIANTS = {
    "nginx_buffering_on": "proxy_buffering on;",
    "nginx_buffering_off": "proxy_buffering off;",
    "nginx_gzip_on_stream": "proxy_buffering off; gzip on; gzip_min_length 1; gzip_types text/event-stream;",
    "nginx_repo_rule": "proxy_buffering off; gzip on; gzip_min_length 256; gzip_types application/json;",
}


def nginx_bin() -> str | None:
    return shutil.which("nginx") or shutil.which("nginx", path="/usr/sbin:/usr/local/sbin")


def nginx_conf(prefix: pathlib.Path, listen: int, upstream: int, body: str) -> str:
    return f"""daemon off; master_process off; pid {prefix}/nginx.pid; error_log {prefix}/error.log warn;
events {{ worker_connections 64; }}
http {{
  access_log off;
  client_body_temp_path {prefix}/cb; proxy_temp_path {prefix}/px; fastcgi_temp_path {prefix}/fc;
  uwsgi_temp_path {prefix}/uw; scgi_temp_path {prefix}/sc;
  server {{
    listen 127.0.0.1:{listen};
    location / {{ {body} proxy_read_timeout 120s; proxy_pass http://127.0.0.1:{upstream}; }}
  }}
}}
"""


def free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


async def one(server_kw: dict, proxy: str | None, scale: float, nginx_body: str | None = None) -> dict:
    led = st.StreamLedger(fakeredis.FakeRedis())
    srv = await st.FakeModelServer(led, ttft_s=0.88 / scale, tok_s=st.TOK_S * scale, **server_kw).start()
    proc = px = None
    try:
        if nginx_body is not None:
            prefix = pathlib.Path(tempfile.mkdtemp(prefix="ngx17-"))
            port = free_port()
            (prefix / "nginx.conf").write_text(nginx_conf(prefix, port, srv.port, nginx_body))
            proc = subprocess.Popen([nginx_bin(), "-p", str(prefix), "-c", str(prefix / "nginx.conf")],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(100):
                try:
                    _, w = await asyncio.open_connection("127.0.0.1", port)
                    w.close()
                    break
                except OSError:
                    await asyncio.sleep(0.05)
        elif proxy is not None:
            px = await st.MiniProxy(srv.port, mode=proxy).start()
            port = px.port
        else:
            port = srv.port
        r = await st.sse_get(port, "talk17-sample-0001")
        return {"first_token_s": r.first_token_s, "first_event_s": r.first_event_s, "done_s": r.done_s,
                "tokens": r.tokens, "ended": r.ended}
    finally:
        if px:
            await px.close()
        if proc:
            proc.terminate()
            proc.wait(5)
        await srv.close()


async def drop_and_resume(scale: float, regenerate: bool) -> dict:
    led = st.StreamLedger(fakeredis.FakeRedis())
    srv = await st.FakeModelServer(led, ttft_s=0.88 / scale, tok_s=st.TOK_S * scale, grace_s=5.0 / scale,
                                   regenerate_on_reconnect=regenerate).start()
    px = await st.MiniProxy(srv.port).start()
    try:
        a = await st.sse_get(px.port, "mobile-drop-0001", drop_after_tokens=140)
        await asyncio.sleep(0.5 / scale)
        b = await st.sse_get(px.port, "mobile-drop-0001", last_id=0 if regenerate else a.last_id)
        await asyncio.sleep(0.2 / scale)
        return {"mode": "regenerate" if regenerate else "resume_from_ledger", "tokens_before_drop": a.tokens,
                "tokens_after_reconnect": b.tokens, "tokens_generated": sum(srv.stats.generated.values()),
                "generations_started": sum(srv.stats.starts.values()),
                "tool_calls_seen_twice": regenerate}
    finally:
        await px.close()
        await srv.close()


async def tab_close(scale: float, propagate: bool) -> dict:
    led = st.StreamLedger(fakeredis.FakeRedis())
    srv = await st.FakeModelServer(led, ttft_s=0.88 / scale, tok_s=st.TOK_S * scale, grace_s=1.0 / scale,
                                   propagate_cancel=propagate).start()
    px = await st.MiniProxy(srv.port, propagate_cancel=propagate).start()
    try:
        a = await st.sse_get(px.port, "tab-close-0001", drop_after_tokens=140)
        await asyncio.sleep(18.0 / scale)
        gen = srv.stats.generated.get("tab-close-0001", 0)
        return {"propagate_cancel": propagate, "tokens_read": a.tokens, "tokens_generated": gen,
                "tokens_saved": st.MEDIAN_TOKENS - gen,
                "grace_s": round(1.0 / scale, 3)}
    finally:
        await px.close()
        await srv.close()


async def stall(scale: float) -> dict:
    led = st.StreamLedger(fakeredis.FakeRedis())
    srv = await st.FakeModelServer(led, ttft_s=0.88 / scale, tok_s=st.TOK_S * scale, grace_s=30.0 / scale).start()
    px = await st.MiniProxy(srv.port, stall_after=3000).start()
    try:
        r = await st.resilient_get(px.port, "stall-0001", idle_timeout_s=2.0 / scale)
        r["idle_timeout_s"] = round(2.0 / scale, 3)
        r["tokens_generated"] = srv.stats.generated.get("stall-0001", 0)
        return r
    finally:
        await px.close()
        await srv.close()


def expired_ledger() -> dict:
    clock = FakeClock()
    led = st.StreamLedger(fakeredis.FakeRedis(), clock=clock, ttl_s=300)
    led.open("old-0001")
    for i in range(1, 141):
        led.append("old-0001", i, "token", f"w{i}")
    clock.advance(301)
    plan = st.resume_plan(led, "old-0001", 140)
    return {"action": plan["action"], "event": plan["events"][0][1], "offer": plan["events"][0][2]["offer"]}


async def main_async(scale: float) -> dict:
    t0 = time.time()
    scen = {
        "incident_service_and_gateway_buffered": await one({"buffered": True, "accel_header": False}, "buffer", scale),
        "service_streams_gateway_buffers": await one({"accel_header": False}, "buffer", scale),
        "gateway_gzips_the_stream": await one({}, "gzip", scale),
        "streamed_end_to_end": await one({}, "pass", scale),
    }
    nginx = nginx_bin()
    ngx = {}
    if nginx:
        for name, body in NGINX_VARIANTS.items():
            ngx[name] = await one({"accel_header": False}, None, scale, nginx_body=body)
        ngx["nginx_buffering_on_but_service_sends_x_accel_no"] = await one({}, None, scale,
                                                                         nginx_body=NGINX_VARIANTS["nginx_buffering_on"])
    ver = subprocess.run([nginx, "-v"], capture_output=True, text=True).stderr.strip() if nginx else None
    return {
        "note": "measured wall-clock through local sockets; the model is a fake with a set speed",
        "scale": scale, "model": {"ttft_s": round(0.88 / scale, 4), "tok_s": st.TOK_S * scale,
                                  "tokens": st.MEDIAN_TOKENS},
        "machine": platform.platform(), "nginx": ver,
        "mini_proxy": scen, "real_nginx": ngx,
        "drop_at_140": [await drop_and_resume(scale, False), await drop_and_resume(scale, True)],
        "tab_close_at_140": [await tab_close(scale, True), await tab_close(scale, False)],
        "stalled_hop": await stall(scale),
        "ledger_expired": expired_ledger(),
        "wall_s": round(time.time() - t0, 1),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", type=float, default=1.0)
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args(argv)
    if a.scale <= 0:
        ap.error("--scale must be positive")
    res = asyncio.run(main_async(a.scale))
    pathlib.Path(a.out).write_text(json.dumps(res, indent=2) + "\n")
    print(json.dumps({k: v for k, v in res.items() if k in ("mini_proxy", "real_nginx")}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
