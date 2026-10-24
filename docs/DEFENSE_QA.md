# Defense Q&A

- **"Your load numbers are fake."** They are simulated, and labelled that way everywhere. They show which way each pattern moves a metric under controlled conditions. The only real-infrastructure numbers are the kind rollout and the sqlite index proof.
- **"Why not just one big model?"** The gate rejects that change (`bad_candidate_deploy: false`). The router halves cost on the mixed set ($0.022275 → $0.010125, simulated).
- **"Is 15 eval scenarios enough?"** No. It is a regression check, not a quality claim, and it misses grounding being disabled (PITFALLS #3).
- **"Why keep a reranker in the code if it's off?"** The keep/drop rule is re-run per corpus. The decision is data, not taste.
- **"Why Redis and not in-process limits?"** Replicas must share state. The cross-replica test fails with per-process buckets.
- **"Would this survive a real outage?"** The breaker and fallback are tested in unit tests and the simulation. A real multi-region failover is not built.
