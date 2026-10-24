# Defense Q&A

- **"Your load numbers are fake."** The load model runs in virtual time with fake providers, and every number from it is labelled simulated. It shows which way each pattern moves a metric. The real-infrastructure numbers are the kind rollout in CI (`loadtest/results/k8s_rollout.json`), the real-Redis and Postgres integration job, and the sqlite index proof.
- **"Why not just one big model?"** Main's intent ladder cut simulated model spend 15.3x (README). The 10M-user cost model shows the per-conversation cost misses its target unless the mix shifts to small models.
- **"Why keep a reranker in the code if it's off?"** The keep/drop rule is re-run per corpus. On this corpus it added about 4 ms and no hit@1 gain, so it is off ([ADR 0008](adr/0008-drop-reranker-by-rule.md)).
- **"Why Redis and not in-process limits?"** Replicas must share state; `test_limits_shared_across_replicas` fails with per-process buckets.
- **"Can one user starve everyone?"** Not of model slots any more: each user holds at most 4 of the cluster cap (`test_one_user_cannot_take_every_model_slot`). See [pitfalls.md](pitfalls.md) for how that was found.
- **"Would this survive a real outage?"** The breaker and fallback are tested in unit tests and the load model. A real multi-region failover is not built.
