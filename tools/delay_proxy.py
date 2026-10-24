"""A local TCP proxy that adds a fixed one-way delay to every chunk, to fake a long network path.

python -m tools.delay_proxy --rtt-ms 100      # measures TLS 1.2 vs 1.3 through the proxy and prints JSON

Each byte waits rtt/2 going in and rtt/2 coming back, so a request/response exchange costs one extra RTT.
The proxy cannot delay the TCP handshake itself (the kernel answers SYNs to the proxy's own socket), so
the TCP connect stays local; docs/network-waterfall.md says so next to the numbers. Standard library plus
the `cryptography` package for a throwaway localhost certificate.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import http.server
import json
import pathlib
import ssl
import tempfile
import threading
import time


class DelayProxy:
    def __init__(self, target: tuple[str, int], rtt_ms: float):
        self.target, self.one_way = target, rtt_ms / 2000
        self.loop = asyncio.new_event_loop()
        self.port = None
        self._ready = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    async def _pipe(self, r: asyncio.StreamReader, w: asyncio.StreamWriter):
        q: asyncio.Queue = asyncio.Queue()

        async def sender():
            while True:
                due, data = await q.get()
                if data is None:
                    break
                await asyncio.sleep(max(0.0, due - time.monotonic()))
                w.write(data)
                await w.drain()
            w.close()

        task = asyncio.create_task(sender())
        try:
            while data := await r.read(65536):
                q.put_nowait((time.monotonic() + self.one_way, data))
        except (ConnectionError, OSError):
            pass
        q.put_nowait((0, None))
        await task

    async def _handle(self, cr, cw):
        sr, sw = await asyncio.open_connection(*self.target)
        await asyncio.gather(self._pipe(cr, sw), self._pipe(sr, cw), return_exceptions=True)

    def _run(self):
        asyncio.set_event_loop(self.loop)

        async def start():
            self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
            self.port = self.server.sockets[0].getsockname()[1]
            self._ready.set()

        self.loop.run_until_complete(start())
        self.loop.run_forever()

    def __enter__(self):
        self._t.start()
        self._ready.wait(5)
        return self

    async def _shutdown(self):
        self.server.close()
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def __exit__(self, *a):
        asyncio.run_coroutine_threadsafe(self._shutdown(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._t.join(5)
        self.loop.close()


def make_cert(d: pathlib.Path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), False)
            .sign(key, hashes.SHA256()))
    c, k = d / "c.pem", d / "k.pem"
    c.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    k.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()))
    return c, k


class _H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True
    body = b"x" * 2000

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *a):
        pass


def measure(rtt_ms: float, tls_version: str, warm: int = 2) -> list[dict]:
    """Cold + warm probes of a local TLS server reached through the delay proxy (real sockets, real TLS)."""
    from tools.latency_probe import Probe
    with tempfile.TemporaryDirectory() as td:
        c, k = make_cert(pathlib.Path(td))
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
        sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        v = ssl.TLSVersion.TLSv1_3 if tls_version == "1.3" else ssl.TLSVersion.TLSv1_2
        sctx.minimum_version = sctx.maximum_version = v
        sctx.load_cert_chain(c, k)
        srv.socket = sctx.wrap_socket(srv.socket, server_side=True)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with DelayProxy(("127.0.0.1", srv.server_address[1]), rtt_ms) as px:
                p = Probe(f"https://localhost:{px.port}/", ssl.create_default_context(cafile=str(c)))
                rows = [p.cold(gzip=False)] + [p.warm(gzip=False) for _ in range(warm)]
                p.close()
        finally:
            srv.shutdown()
    return [{k2: round(v2, 1) if isinstance(v2, float) else v2 for k2, v2 in r.items()} for r in rows]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rtt-ms", type=float, default=100)
    a = ap.parse_args()
    print(json.dumps({v: measure(a.rtt_ms, v) for v in ("1.2", "1.3")}, indent=1))


if __name__ == "__main__":
    main()
