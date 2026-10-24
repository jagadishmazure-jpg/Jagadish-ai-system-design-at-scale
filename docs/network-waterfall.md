# Talk 18 (B): 40 ms on the server, seconds in the browser

Source: a public talk on AI system design, credited by topic only and written in my own words. This page extends [networking-journey.md](networking-journey.md) and `tools/latency_probe.py` and does not repeat them.

## The story (fictional)

A founder demos the product from a hotel abroad. The page takes about 4 s to appear, where it took about 300 ms in the office. The backend logs say the request took 40 ms. Where did the rest go?

## Two kinds of numbers on this page

- **Simulated** (`sysdesign/ext/waterfall.py`, `docs/results/talk18.json`, drift-checked by a test): a round-trip model on a virtual clock. 200 KB page, 40 ms server time, initial congestion window of 10 segments, 1,460-byte segments.
- **Measured** (`tools/delay_proxy.py`, `docs/results/talk18_measured.json`): real TLS handshakes on this machine through a local proxy that delays every chunk by half the RTT in each direction.
- **Quoted**: the talk's illustrative 120 ms (office) and 1,140 ms (hotel). They are shown for comparison only, not as our results.

## The waterfall (simulated)

![Network waterfall](img/network_waterfall.png)

| Path | RTT | DNS | TCP | TLS | Request | Server | Transfer | Total | Server share |
|---|---|---|---|---|---|---|---|---|---|
| Office (DNS cached, TLS 1.3, 200 Mbps) | 10 ms | 0 | 10 | 10 | 10 | 40 | 38 | **108 ms** | 37.0% |
| Hotel, cold (DNS 4 RTT, TLS 1.2, 10 Mbps) | 200 ms | 800 | 200 | 400 | 200 | 40 | 760 | **2,400 ms** | 1.7% |
| Hotel, DNS cached + TLS 1.3 | 200 ms | 0 | 200 | 200 | 200 | 40 | 760 | **1,400 ms** | 2.9% |
| Hotel, warm kept-alive connection | 200 ms | 0 | 0 | 0 | 200 | 40 | 760 | **1,000 ms** | 4.0% |
| Hotel via edge server (20 ms to edge, warm link to origin 180 ms) | 20 ms | 0 | 20 | 20 | 20 + 180 | 40 | 220 | **500 ms** | 8.0% |
| *Talk, quoted (illustrative)* | | | | | | | | *120 ms vs 1,140 ms* | *40/1,140 = 3.5%* |

The model's office figure (108 ms) is close to the quoted 120 ms. The hotel figure depends on the assumptions: 1,400 ms with TLS 1.3 and cached DNS, 2,400 ms fully cold. Either way the server is 1.7% to 4% of a hotel page load. That matches the talk's point that the server is about 3% of the wait.

**Slow start.** 200 KB is 137 segments. The window grows 10, 20, 40, 80, so delivery takes 4 rounds of data. The first round rides with the first byte, and each of the other 3 costs a full RTT. At 200 ms that is 600 ms of waiting that more bandwidth does not fix. Fewer round trips, or shorter ones, do.

**The edge server.** The browser does TCP and TLS with a server 20 ms away. That server keeps an already-open connection to the origin, whose window has already grown, so the long 180 ms hop is paid once per request and with no slow start. Hotel time drops from 1,400 ms to 500 ms in the model.

## Measured on this machine (through the delay proxy, injected RTT 100 ms)

| | TLS handshake | Request on the new connection | Warm request (reused connection) |
|---|---|---|---|
| TLS 1.2 | 204.4 ms (2 RTT) | 101.8 ms | 101.5 ms |
| TLS 1.3 | 102.6 ms (1 RTT) | 101.6 ms | 101.3 ms |

This confirms the model's round-trip counts with real sockets and real TLS: 1.2 needs two round trips and 1.3 needs one. A reused connection pays only the request round trip. One caveat: a user-space proxy cannot delay the kernel's TCP handshake to the proxy itself, so TCP connect stays at about 0.2 ms in these rows. The model counts it as 1 RTT. Run it yourself with `python -m tools.delay_proxy --rtt-ms 100`.

## The method

1. Break the request into phases. 2. Measure each one (model, proxy, or `latency_probe` against a real host). 3. Circle the biggest bar. 4. Fix it, measure again, repeat. In the hotel the biggest bars are round trips (DNS, handshakes, slow start), so the fixes are TLS 1.3, keep-alive, DNS caching, a bigger initial window and an edge server. Server tuning is last.

## Interview points

- Count round trips before you count milliseconds. Total time is roughly the number of round trips times the RTT, plus server time.
- "The server took 40 ms" and "the page took 4 s" can both be true. Ask where the user is.
- Bandwidth fixes transfer of big files. RTT fixes everything else, and slow start makes even big files mostly an RTT problem.
