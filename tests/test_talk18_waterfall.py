"""Talk 18 (B): office vs hotel page load, round-trip model plus a real delay proxy."""
import json
import pathlib
import subprocess
import sys

import pytest

from sysdesign.ext.waterfall import (
    HOTEL_13,
    HOTEL_COLD,
    HOTEL_WARM,
    OFFICE,
    QUOTED,
    Path,
    edge,
    results,
    slow_start_rounds,
    waterfall,
)
from tools.delay_proxy import DelayProxy, measure

ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("nbytes,rounds", [(1460, 1), (14600, 1), (14601, 2), (200_000, 4), (1_000_000, 7)])
def test_slow_start_rounds(nbytes, rounds):
    assert slow_start_rounds(nbytes) == rounds


def test_bigger_initial_window_means_fewer_rounds():
    assert slow_start_rounds(200_000, init_cwnd=40) < slow_start_rounds(200_000, init_cwnd=10)


def test_tls12_costs_one_more_rtt_than_tls13():
    a = waterfall(Path("x", 100, 1000, tls="1.2"))["total_ms"]
    b = waterfall(Path("x", 100, 1000, tls="1.3"))["total_ms"]
    assert a - b == pytest.approx(100)


def test_cold_dns_costs_four_rtts():
    w = waterfall(Path("x", 50, 1000, dns_cached=False))
    assert w["phases_ms"]["dns"] == 200


def test_reused_connection_skips_tcp_and_tls():
    p = waterfall(HOTEL_WARM)["phases_ms"]
    assert p["tcp"] == 0 and p["tls"] == 0 and p["dns"] == 0


def test_office_near_talks_quoted_figure():
    assert abs(waterfall(OFFICE)["total_ms"] - QUOTED["office_ms"]) < 30


def test_hotel_far_slower_than_office():
    assert waterfall(HOTEL_COLD)["total_ms"] > 10 * waterfall(OFFICE)["total_ms"]


def test_server_share_small_in_hotel():
    assert waterfall(HOTEL_COLD)["server_share"] < 0.05 and waterfall(HOTEL_13)["server_share"] < 0.05


def test_transfer_dominates_hotel_with_tls13_cached():
    ph = waterfall(HOTEL_13)["phases_ms"]
    assert max(ph, key=ph.get) == "transfer"


def test_edge_beats_direct_hotel():
    assert edge(20, 180, 10)["total_ms"] < waterfall(HOTEL_13)["total_ms"] / 2


def test_edge_pays_origin_rtt_once():
    assert edge(20, 180, 10)["phases_ms"]["edge_to_origin"] == 180


def test_results_rows_and_quote_labelled():
    r = results()
    assert "quoted" in r["quoted"]["source"] and len(r["rows"]) == 5
    assert r["slow_start_windows"] == [10, 20, 40, 80]
    assert r["quoted_server_share"] == pytest.approx(40 / 1140, abs=1e-4)


def test_committed_results_match_fresh_run():
    committed = json.loads((ROOT / "docs/results/talk18.json").read_text())
    assert committed["waterfall"] == json.loads(json.dumps(results()))


def test_bench_check_script_passes():
    out = subprocess.run([sys.executable, "scripts/talk18_bench.py", "--check"], cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0, out.stdout


# ---- real sockets through the delay proxy (lower bounds only, so slow CI cannot flake them)

def test_delay_proxy_adds_at_least_one_rtt_per_exchange():
    import socket
    import threading
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()

    def echo():
        c, _ = srv.accept()
        c.sendall(c.recv(10))
        c.close()
    threading.Thread(target=echo, daemon=True).start()
    import time
    with DelayProxy(srv.getsockname(), rtt_ms=80) as px:
        s = socket.create_connection(("127.0.0.1", px.port))
        t = time.perf_counter()
        s.sendall(b"hi")
        assert s.recv(10) == b"hi"
        assert time.perf_counter() - t >= 0.075
        s.close()
    srv.close()


@pytest.fixture(scope="module")
def tls_rows():
    return {v: measure(60, v, warm=1) for v in ("1.2", "1.3")}


def test_real_tls13_handshake_is_one_rtt(tls_rows):
    r = tls_rows["1.3"][0]
    assert r["tls_version"] == "TLSv1.3" and 55 <= r["tls_ms"] < 115


def test_real_tls12_handshake_is_two_rtts(tls_rows):
    r = tls_rows["1.2"][0]
    assert r["tls_version"] == "TLSv1.2" and r["tls_ms"] >= 115


def test_real_warm_request_costs_one_rtt_and_no_handshake(tls_rows):
    w = tls_rows["1.3"][1]
    assert w["tls_ms"] == 0 and w["ttfb_ms"] >= 55
