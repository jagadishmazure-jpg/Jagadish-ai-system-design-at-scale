# Networking journey: what happens before the first byte

A request has to pass **DNS → TCP → TLS → HTTP**. `scripts/netcheck.py` tests each layer in order and reports the first one that fails, so "the site is down" turns into "DNS works, TCP to port 443 refused".

- **DNS.** Name → address. `tools/latency_probe.py` and `netcheck` try *every* resolved address. An early bug was that `localhost` resolved to `::1` first and the probe gave up.
- **TCP.** A three-way handshake costs one round trip. Keep-alive avoids paying it per request (cold vs warm in `test_probe_cold_vs_warm_and_compression`).
- **Nagle and delayed ACK (a real finding).** The warm-connection test was flaky. Time to first byte sometimes stuck at about 44 ms instead of about 1 ms. The test server wrote headers and body in separate small writes, and Nagle's algorithm on the server combined with delayed ACK on the client added a roughly 40 ms stall. The fix was `TCP_NODELAY` on both sides. After the fix the test passed 8 runs out of 8, where before it failed about 1 run in 4.
- **TLS 1.3.** One round trip. nginx is set to TLS 1.3 only (`deploy/nginx/nginx.conf`). The test spins up a local TLS 1.3 server with a throwaway cert.
- **HTTP.** gzip and keepalive at nginx. gzip covers `application/json` only; `text/event-stream` is left out on purpose (talk 17), and SSE has buffering off. Admin paths return 404 at the edge. Brotli is **planned**.
- **Segmentation.** Main's compose stack uses one network today, so data-tier segmentation is **planned** there. The Terraform module shows the target layout with an NSG (validated, never applied). `test_loopback_bind_is_unreachable_from_other_interfaces` shows that binding to 127.0.0.1 is not reachable from outside.
