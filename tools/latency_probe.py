"""Break one HTTPS request into DNS, TCP connect, TLS handshake, TTFB and transfer; cold vs warm.

python -m tools.latency_probe https://example.org/ --repeat 3
Cold = new DNS lookup + new TCP + new TLS. Warm = the same kept-alive connection reused, so only
TTFB + transfer remain. Uses only the standard library.
"""
from __future__ import annotations

import argparse
import json
import socket
import ssl
import time
from urllib.parse import urlsplit


class Probe:
    def __init__(self, url: str, ctx: ssl.SSLContext | None = None, timeout: float = 5.0) -> None:
        u = urlsplit(url)
        self.host, self.scheme = u.hostname, u.scheme
        self.port = u.port or (443 if u.scheme == "https" else 80)
        self.path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        self.ctx = ctx or ssl.create_default_context()
        self.ctx.minimum_version = max(self.ctx.minimum_version, ssl.TLSVersion.TLSv1_2)
        self.timeout = timeout
        self.sock = None
        self.addr = None

    def _request(self, gzip: bool) -> tuple[float, float, int, dict]:
        enc = "gzip, br" if gzip else "identity"
        req = (f"GET {self.path} HTTP/1.1\r\nHost: {self.host}\r\nAccept-Encoding: {enc}\r\n"
               f"Connection: keep-alive\r\n\r\n").encode()
        t0 = time.perf_counter()
        self.sock.sendall(req)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("closed before headers")
            buf += chunk
        ttfb = time.perf_counter() - t0
        head, body = buf.split(b"\r\n\r\n", 1)
        headers = {}
        for line in head.decode("latin-1").split("\r\n")[1:]:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
        if "content-length" in headers:
            need = int(headers["content-length"])
            while len(body) < need:
                body += self.sock.recv(65536)
        elif headers.get("transfer-encoding") == "chunked":
            while not body.endswith(b"0\r\n\r\n"):
                body += self.sock.recv(65536)
        transfer = time.perf_counter() - t0 - ttfb
        return ttfb, transfer, len(body), headers

    def cold(self, gzip: bool = True) -> dict:
        t0 = time.perf_counter()
        infos = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM)
        dns = time.perf_counter() - t0
        t1 = time.perf_counter()
        raw, err = None, None
        for info in infos:  # try each address (e.g. ::1 then 127.0.0.1) like a real client
            try:
                raw = socket.create_connection(info[4][:2], timeout=self.timeout)
                raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self.addr = info[4]
                break
            except OSError as e:
                err = e
        if raw is None:
            raise err or OSError("no address")
        tcp = time.perf_counter() - t1
        tls, version = 0.0, None
        if self.scheme == "https":
            t2 = time.perf_counter()
            raw = self.ctx.wrap_socket(raw, server_hostname=self.host)
            tls = time.perf_counter() - t2
            version = raw.version()
        self.sock = raw
        ttfb, transfer, size, headers = self._request(gzip)
        return {"phase": "cold", "dns_ms": dns * 1e3, "tcp_ms": tcp * 1e3, "tls_ms": tls * 1e3, "tls_version": version,
                "ttfb_ms": ttfb * 1e3, "transfer_ms": transfer * 1e3, "bytes": size,
                "content_encoding": headers.get("content-encoding", "identity"),
                "total_ms": (dns + tcp + tls + ttfb + transfer) * 1e3}

    def warm(self, gzip: bool = True) -> dict:
        ttfb, transfer, size, headers = self._request(gzip)
        return {"phase": "warm", "dns_ms": 0.0, "tcp_ms": 0.0, "tls_ms": 0.0, "ttfb_ms": ttfb * 1e3,
                "transfer_ms": transfer * 1e3, "bytes": size,
                "content_encoding": headers.get("content-encoding", "identity"), "total_ms": (ttfb + transfer) * 1e3}

    def close(self) -> None:
        if self.sock:
            self.sock.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--insecure", action="store_true", help="skip cert checks (local dev certs only)")
    a = ap.parse_args()
    ctx = ssl.create_default_context()
    if a.insecure:
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    p = Probe(a.url, ctx)
    rows = [p.cold()] + [p.warm() for _ in range(a.repeat)]
    p.close()
    print(json.dumps([{k: round(v, 2) if isinstance(v, float) else v for k, v in r.items()} for r in rows], indent=1))


if __name__ == "__main__":
    main()
