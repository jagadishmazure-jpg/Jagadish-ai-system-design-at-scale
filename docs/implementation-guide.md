# Implementation guide (for adopters)

1. Start with the gateway and admission (`gateway.py`, `admission.py`). Size `Limits` from your provider quota, and add per-user lease caps (see PITFALLS #1).
2. Add the cache with tenant partitioning before you add the router. The cache is the cheaper win when questions repeat.
3. Write 10–20 eval scenarios from real tickets, gate releases on them in CI, and add a scenario for every incident.
4. Wire `requests_total{path,status}` on every route *before* writing alerts. Keep `test_alert_rules_only_use_metrics_the_app_exports`.
5. Deploy with `maxUnavailable: 0` and a readiness probe that checks config and dependencies, not just "the process is up".
6. Keep the desired version in git and make rollback a revert.
7. Replace the fake providers in `llm.py` with real clients. Keep the breaker and fallback around them.
8. Before any cloud apply: list the resources, estimate the cost, get approval.
