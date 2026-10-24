# Pitfalls and known failures (kept visible)

1. **Tenant-wide concurrency is not fair across users (open).** At full simulated scale (40 users × 10 requests, plus an abuser sending 600), with `max_concurrent_model_calls=16`, normal users succeed only 31.5% of the time. The rejections are `concurrency` 429s, not server errors. Fix (planned): per-user lease caps plus a cap sized to the provider quota. `results/loadtest_full.json`.
2. **Anomaly in the noisy-neighbour baseline at full scale (open).** With admission off, the 1,000 requests made only 58 provider calls and normal p95 was 1.83 ms. Something upstream of the provider is answering most requests without a model call. Until that is explained, the full-scale "off" baseline should not be read as a fair comparison. The ci-scale run (115 provider calls) is the one quoted in the README.
3. **The eval suite does not catch grounding being disabled.** Turning grounding off leaves the suite at 1.0, because no scenario tempts the model to invent a number. Grounding is covered by its own test (`test_grounding_blocks_invented_number`). A suite scenario is planned.
4. **The gateway replay killed streaming (fixed).** Body replay sent `http.disconnect` immediately, which ended SSE responses early. The fix was to defer to the real `receive()`.
5. **Alert rules named metrics that did not exist (fixed).** They referenced `http_requests_total` and `chat_latency_seconds`. `test_alert_rules_only_use_metrics_the_app_exports` now fails on any such drift. Only `/v1/chat` 200s were counted before; now every route and status is.
6. **Nagle and delayed-ACK flakiness (fixed).** See NETWORKING_JOURNEY.

## Four stress questions
- **10x traffic tomorrow?** Cost is linear (`--scale 10`), so the router and cache matter first. Admission caps protect the provider quota. The queue autoscaler scales on age.
- **The primary model provider is down?** The breaker opens and the fallback serves. Error rate went from 0.3375 to 0.0 (simulated).
- **One customer floods us?** The per-user bucket gives the abuser 429s at a rate of 0.875 (ci) while normal-user p95 improves. See pitfall 1 for the limit.
- **A bad release?** Readiness blocks it in k8s. If it gets through, the 1% 5xx alert leads to a git revert.
