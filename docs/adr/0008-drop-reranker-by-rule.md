# ADR 0008: Keep the reranker only if it earns its latency
**Status:** accepted. **Decision:** keep it if hit@1 improves by at least 0.05 at no more than 10 ms extra p50. Measured: +0.0 hit@1 at about +4.2 ms, so it is off. **Consequences:** re-run the benchmark whenever the corpus changes.
