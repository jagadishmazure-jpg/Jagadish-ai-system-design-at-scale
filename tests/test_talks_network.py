import datetime
import gzip
import http.server
import socket
import ssl
import threading

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from scripts.netcheck import check
from tools.latency_probe import Probe

BODY = b'{"status": "ok", "pad": "' + b"kestrel " * 400 + b'"}'


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True  # without this, header+body writes hit Nagle + delayed ACK (~40 ms)

    def do_GET(self):  # noqa: N802
        data = BODY
        self.send_response(200)
        if "gzip" in self.headers.get("Accept-Encoding", ""):
            data = gzip.compress(BODY)
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def _cert(tmp_path):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), False)
            .sign(key, hashes.SHA256()))
    c, k = tmp_path / "c.pem", tmp_path / "k.pem"
    c.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    k.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()))
    return c, k


@pytest.fixture
def tls_server(tmp_path):
    c, k = _cert(tmp_path)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    sctx.minimum_version = ssl.TLSVersion.TLSv1_3
    sctx.load_cert_chain(c, k)
    srv.socket = sctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cctx = ssl.create_default_context(cafile=str(c))
    yield f"https://localhost:{srv.server_address[1]}/healthz", cctx
    srv.shutdown()


def test_probe_cold_vs_warm_and_compression(tls_server):
    url, ctx = tls_server
    p = Probe(url, ctx)
    cold = p.cold(gzip=True)
    warm = p.warm(gzip=True)
    plain = p.warm(gzip=False)
    p.close()
    assert cold["tls_version"] == "TLSv1.3" and cold["tls_ms"] > 0 and cold["tcp_ms"] > 0
    assert warm["tls_ms"] == 0 and warm["total_ms"] < cold["total_ms"]
    assert cold["content_encoding"] == "gzip" and cold["bytes"] < plain["bytes"] / 5


def test_netcheck_reports_first_failing_layer(tls_server, monkeypatch):
    url, ctx = tls_server
    assert check(url, ctx=ctx)["first_failure"] is None
    assert check(url, ctx=ssl.create_default_context())["first_failure"] == "tls"  # untrusted cert
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    closed = s.getsockname()[1]
    s.close()
    assert check(f"http://127.0.0.1:{closed}/")["first_failure"] == "tcp"

    def nxdomain(*a, **k):
        raise socket.gaierror("Name or service not known")
    monkeypatch.setattr(socket, "getaddrinfo", nxdomain)  # some sandboxes wildcard-resolve every name
    assert check("http://no-such-host.invalid/")["first_failure"] == "dns"


def _lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
    except OSError:
        ip = None
    finally:
        s.close()
    return None if not ip or ip.startswith("127.") else ip


def test_loopback_bind_is_unreachable_from_other_interfaces():
    ip = _lan_ip()
    if ip is None:
        pytest.skip("no non-loopback interface")
    for bind, reachable in (("127.0.0.1", False), ("0.0.0.0", True)):  # noqa: S104
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((bind, 0))
        srv.listen()
        port = srv.getsockname()[1]
        try:
            socket.create_connection((ip, port), timeout=1).close()
            ok = True
        except OSError:
            ok = False
        srv.close()
        assert ok is reachable, bind
