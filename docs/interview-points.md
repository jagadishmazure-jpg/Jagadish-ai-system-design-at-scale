# Interview points

- A turn can look cheap while the conversation is over budget ($0.003375 a turn vs $0.0135 a conversation).
- Inference is about 95% of latency (18 ms of 19 ms, simulated). Optimise model calls, not the gateway.
- An index turns SCAN into SEARCH: 6.83 ms → 0.0052 ms over 200k rows (real sqlite).
- A reranker that adds 4.26 ms for +0.0 hit@1 gets dropped by rule.
- A tenant-wide concurrency cap protects the provider but not fairness. I measured it shedding 68.5% of normal requests at full simulated scale.
- Alerts that reference non-existent metrics never fire. I found and fixed exactly that, and added a test for it.
- A ~40 ms flaky TTFB turned out to be Nagle plus delayed ACK. TCP_NODELAY fixed it.
- Readiness plus maxUnavailable 0 turns a broken release into a stalled rollout instead of an outage (kind, CI).
