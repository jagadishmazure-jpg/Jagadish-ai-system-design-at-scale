"""Talk 18 (B): where the other seconds go. A round-trip model of one page load, office vs hotel vs edge.

This is a SIMULATION on a virtual clock: every phase is a count of round trips times the RTT, plus the
server's own time and the time to push the bytes through the link. tools/delay_proxy.py checks the
handshake round-trip counts against real sockets on this machine (see docs/network-waterfall.md).

Model, per phase:
  dns       0 if cached, else `dns_rtts` resolver round trips (a cold recursive lookup)
  tcp       1 RTT (SYN, SYN-ACK; the ACK rides with the first data)
  tls       TLS 1.3 = 1 RTT, TLS 1.2 = 2 RTT (0 when the connection is reused)
  ttfb      1 RTT for the request + server time
  transfer  TCP slow start: the congestion window starts at `init_cwnd` segments and doubles each round
            trip; the first window arrives with the first byte, every later window costs 1 more RTT,
            plus serialisation time on the link.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

MSS = 1460  # bytes per segment
QUOTED = {"office_ms": 120, "hotel_ms": 1140, "source": "quoted from the talk, illustrative"}


@dataclass(frozen=True)
class Path:
    name: str
    rtt_ms: float
    bandwidth_mbps: float
    dns_cached: bool = True
    dns_rtts: int = 4
    tls: str = "1.3"  # "1.2", "1.3" or "reused"
    tcp_reused: bool = False


def slow_start_rounds(nbytes: int, init_cwnd: int = 10) -> int:
    """Round trips of data needed to deliver nbytes when the window doubles from init_cwnd segments."""
    segs = math.ceil(nbytes / MSS)
    rounds, cwnd, sent = 0, init_cwnd, 0
    while sent < segs:
        sent += cwnd
        cwnd *= 2
        rounds += 1
    return rounds


def waterfall(p: Path, page_bytes: int = 200_000, server_ms: float = 40.0, init_cwnd: int = 10) -> dict:
    rtt = p.rtt_ms
    dns = 0.0 if p.dns_cached else p.dns_rtts * rtt
    tcp = 0.0 if p.tcp_reused else rtt
    tls = {"1.3": 1, "1.2": 2, "reused": 0}[p.tls] * rtt
    rounds = slow_start_rounds(page_bytes, init_cwnd)
    transfer = (rounds - 1) * rtt + page_bytes * 8 / (p.bandwidth_mbps * 1000)  # ms
    phases = {"dns": dns, "tcp": tcp, "tls": tls, "request_rtt": rtt, "server": server_ms, "transfer": transfer}
    total = sum(phases.values())
    return {"path": asdict(p), "phases_ms": {k: round(v, 1) for k, v in phases.items()},
            "slow_start_rounds": rounds, "total_ms": round(total, 1),
            "server_share": round(server_ms / total, 4)}


def edge(client_rtt_ms: float, origin_rtt_ms: float, bandwidth_mbps: float, page_bytes: int = 200_000,
         server_ms: float = 40.0, init_cwnd: int = 10, dns_cached: bool = True) -> dict:
    """An edge server near the user ends TCP and TLS there and keeps a warm (already open, window already
    grown) connection to the origin, so the long hop costs one round trip per request and no slow start."""
    rtt = client_rtt_ms
    dns = 0.0 if dns_cached else 4 * rtt
    phases = {"dns": dns, "tcp": rtt, "tls": rtt, "request_rtt": rtt, "edge_to_origin": origin_rtt_ms,
              "server": server_ms}
    rounds = slow_start_rounds(page_bytes, init_cwnd)
    phases["transfer"] = (rounds - 1) * rtt + page_bytes * 8 / (bandwidth_mbps * 1000)
    total = sum(phases.values())
    return {"phases_ms": {k: round(v, 1) for k, v in phases.items()}, "slow_start_rounds": rounds,
            "total_ms": round(total, 1), "server_share": round(server_ms / total, 4)}


OFFICE = Path("office", rtt_ms=10, bandwidth_mbps=200, dns_cached=True, tls="1.3")
HOTEL_COLD = Path("hotel, cold", rtt_ms=200, bandwidth_mbps=10, dns_cached=False, tls="1.2")
HOTEL_13 = Path("hotel, TLS 1.3 + DNS cached", rtt_ms=200, bandwidth_mbps=10, dns_cached=True, tls="1.3")
HOTEL_WARM = Path("hotel, warm connection", rtt_ms=200, bandwidth_mbps=10, tls="reused", tcp_reused=True)


def results() -> dict:
    rows = {p.name: waterfall(p) for p in (OFFICE, HOTEL_COLD, HOTEL_13, HOTEL_WARM)}
    rows["hotel via edge (RTT 20ms, warm origin)"] = edge(client_rtt_ms=20, origin_rtt_ms=180, bandwidth_mbps=10)
    return {"note": "simulated round-trip model, virtual clock; 200 KB page, 40 ms server, init cwnd 10",
            "quoted": QUOTED, "quoted_server_share": round(40 / QUOTED["hotel_ms"], 4),
            "slow_start_windows": [10 * 2 ** i for i in range(slow_start_rounds(200_000))],
            "rows": rows}
