"""Walk the layers DNS -> TCP -> TLS -> HTTP for a URL and report the FIRST layer that fails.

python scripts/netcheck.py http://127.0.0.1:8000/healthz
"""
from __future__ import annotations

import json
import socket
import ssl
import sys
import urllib.request
from urllib.parse import urlsplit


def check(url: str, timeout: float = 2.0, ctx: ssl.SSLContext | None = None) -> dict:
    u = urlsplit(url)
    host, port = u.hostname, u.port or (443 if u.scheme == "https" else 80)
    res = {"url": url, "layers": {}, "first_failure": None}

    def fail(layer, err):
        res["layers"][layer] = f"FAIL: {err}"
        res["first_failure"] = layer
        return res
    try:
        addrs = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        res["layers"]["dns"] = f"ok {addrs[0][4][0]}"
    except OSError as e:
        return fail("dns", e)
    s, err = None, None
    for a in addrs:
        try:
            s = socket.create_connection((a[4][0], port), timeout=timeout)
            break
        except OSError as e:
            err = e
    if s is None:
        return fail("tcp", err)
    res["layers"]["tcp"] = "ok"
    if u.scheme == "https":
        try:
            tls_ctx = ctx or ssl.create_default_context()
            tls_ctx.minimum_version = ssl.TLSVersion.TLSv1_2  # never negotiate TLS 1.0/1.1
            s = tls_ctx.wrap_socket(s, server_hostname=host)
            res["layers"]["tls"] = f"ok {s.version()}"
        except (OSError, ssl.SSLError) as e:
            s.close()
            return fail("tls", e)
    s.close()
    try:
        handlers = [urllib.request.HTTPSHandler(context=ctx)] if ctx else []
        with urllib.request.build_opener(*handlers).open(url, timeout=timeout) as r:
            res["layers"]["http"] = f"ok {r.status}"
    except Exception as e:  # noqa: BLE001 - report any HTTP-layer failure
        return fail("http", e)
    return res


if __name__ == "__main__":
    out = check(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000/healthz")
    print(json.dumps(out, indent=1))
    sys.exit(1 if out["first_failure"] else 0)
