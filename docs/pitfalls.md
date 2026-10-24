# Pitfalls found while porting (kept visible)

Both findings below came from the load harness on the `grokbot/full-build` branch, not from main's load model. I re-checked each against main before deciding what to do.

## 1. The "admission off" baseline counted canned answers as successes (explained; main not affected)

On the branch, the full-scale noisy-neighbour run with admission off reported 1,000 requests, 100% normal-user success, p95 1.83 ms and only 58 provider calls. That looked too good, and it was.

**Cause.** I wrapped the harness client and classified every 200 response. All 1,000 were the canned "can't answer reliably right now" message. The provider quota in that run (hard capacity 24) was exceeded by the first burst of 1,000 concurrent requests, calls failed or were refused by the breaker, and the branch agent returned its degraded message with HTTP **200**. The harness counted any 200 as a success, so the baseline was measuring a fast apology. The 58 provider calls fit that picture (calls before the breaker opened, plus probes), but I did not trace each one.

**Main.** Main's agent returns **503** for a degraded answer (`test_all_providers_down_degrades_honestly_and_refunds_budget`) and main's harness counts every 5xx as an error. So main's load-model numbers do not have this problem and are unchanged by this PR (`python -m loadtest.harness --check` passes).

**What this means for the branch numbers.** The branch's full-scale "off" baseline is not a valid comparison and is not quoted anywhere on main.

## 2. "Admission sheds 68.5% of normal requests" (root-caused; fairness fixed on main)

On the branch, at full scale (40 users in parallel, plus one user sending 600 requests, tenant cap of 16 concurrent model calls) normal users succeeded only 31.5% of the time. A rerun while investigating gave 53.5%, because the result depends on asyncio scheduling order. Either way, most normal rejections were concurrency 429s.

**Cause.** Two things together:
1. The cap was sized below honest demand. 40 users each with a request in flight need more than 16 slots, so the cap rejected (instead of queueing) normal traffic even with no abuser.
2. The cap was shared by everyone with no per-user share. Whoever arrived first held the slots, so one user's parallel burst could take every slot between their token-bucket refills.

**Main.** Main has the same kind of cluster-wide model-call cap (32 slots). Its load model does not hit it, because the per-user token bucket stops the flooder first and the model schedules requests per replica slot. The second cause did apply to main's code, though: one user with parallel calls could hold all 32 slots.

**Fix on main.** `Admission.acquire_model_slot(tenant, user)` now takes a per-user share (`max_user_model_calls`, default 4) inside the cluster cap, in one Lua script so replicas cannot race. `test_one_user_cannot_take_every_model_slot` shows a flooding user capped at its share while other users still get the remaining slots. Main's load-model numbers are unchanged.

**Not fixed.** Sizing the cap to the provider quota, and queueing briefly instead of rejecting at the cap, are still open. The branch harness that produced 68.5% was not ported, so there is no new "after" number for that scenario. It would need main's harness to model parallel in-flight calls per user first.

## 3. Smaller ones (fixed on the branch, carried over)
- **Alert rules named metrics that did not exist.** `test_alerts_and_dashboard_only_use_metrics_the_app_exports` now checks both `deploy/alerts.yml` and the Grafana dashboard against the metrics the app registers.
- **Nagle plus delayed ACK made a latency test flaky (~40 ms stalls).** The probe sets `TCP_NODELAY`. See [networking-journey.md](networking-journey.md).
- **A lazy import in the batching benchmark pointed at a module that only existed on the branch.** No test covered it; running `python -m loadtest.talks_benchmarks` on main found it.
