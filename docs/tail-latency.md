# Talk 16: tail latency and resilience

Everything here is a **simulation** in virtual time with seeded randomness. No real vendor, model or store is called. The numbers come from `python scripts/tail_latency_bench.py` and `python scripts/tail_evidence_board.py`; CI fails if the committed results drift from a fresh run. Source: a public talk on AI system design, credited by topic only and paraphrased in my own words.

## The story (fictional)

Kestrel answers merchants on the made-up Tidewell storefront platform. For a week it felt slow: some replies took twice as long, a few never arrived. There was no deploy and no traffic change, and the model, the database and every dashboard looked fine. The single odd signal was the outside moderation service. Its typical (p50) time had not moved, but its slowest 1% (p99) had jumped from about 300 ms to about 4 s.

Two fixes were proposed first. "One percent can't matter, look at the model." And "the service is slow, add replicas." The replay below tests both.

![Tail latency evidence board](img/tail_evidence_board.png)

## How one turn works

Each turn makes six calls, one after another: load the session, look up the order, retrieve documents, moderate the input, call the model, moderate the output. Their typical times add up to 2.78 s. Every call can wait up to 30 s and is retried 3 times. One replica has 100 workers, traffic is 400 turns a minute, and a turn that takes over 20 s counts as a miss.

## What the replay measured (20,000 turns per scenario)

| | healthy | incident | incident, calls in parallel | incident, all fixes |
|---|---|---|---|---|
| Moderation p99 | 0.302 s | 4.624 s | 4.624 s | 4.624 s |
| Turn p50 | 2.844 s | 2.871 s | 2.421 s | 2.401 s |
| Turn p95 | 4.064 s | 4.252 s | 3.794 s | 3.591 s |
| Turn p99 | 4.716 s | 10.236 s | 9.83 s | 4.319 s |
| Missed the 20 s deadline | 0.00% | 0.32% | 0.29% | 0.00% |
| Extra calls from hedging | - | - | - | 4.19% |

Honest gap: the talk's numbers were a 14 s p99 and 8% of turns missing. This replay gives 10.2 s and 0.32%. At 400 turns a minute only about 20 turns are in flight, so 100 workers never fill while the slow path hits just 1% of moderation calls; the large miss rate needs workers to pile up, which the brownout section shows.

### Why 1% is not small

If each call is slow with chance p, a turn of n calls hits at least one slow call with chance 1 - (1 - p)^n. With p = 1%, two moderation calls make that 1.99% of turns, ten calls make it 9.56%, and a hundred make it 63.4%. Panel (a) shows the formula and a simulation side by side; they agree within one percentage point at every point.

### Why parallel calls are not enough

Order lookup, retrieval and input moderation do not need each other, so they can run together. That cut the median from 2.868 s to 2.414 s, but the p99 only moved from 10.68 s to 10.134 s, and the p99.9 from 29.94 s to 29.10 s. A parallel group finishes when its slowest member finishes, so the tail still belongs to the slow call.

### Why more replicas only buy time (Little's law)

The average number of requests in flight equals arrival rate times time held: L = lambda x W. Now suppose moderation hangs on every call (a brownout from second 10). With a 30 s timeout, 6.67 turns a second x 30 s = 200 in flight against 100 workers; counting 3 retries it is 800. One replica filled all 100 workers 14.5 s after the brownout began. Two replicas filled all 200 workers 29.9 s after: a second replica moved the wall by about 15 s and changed nothing else (93.6% of turns missed in both runs).

## The fixes

| Fix | Where | Measured effect |
|---|---|---|
| Breaker that also opens on slow calls (over 50% of the last 20 slower than 1 s), keeping the failure-count trip and half-open probes | `SlowCallBreaker` in `sysdesign/resilience.py` | In the brownout it opened (here by its failure trip, since budget timeouts turn hangs into failures). Unit tests show it opens on slow calls that succeed, where the old breaker stays closed |
| One bulkhead per dependency, sized as calls per turn x turns per second x accepted latency x slack | `bulkhead_size`, `Bulkhead` in `ext/tail_latency.py` | Moderation: 2 x 6.67 x 0.3 s x 3 = 12 slots. Bulkhead alone, no breaker: 491 calls turned away to the local filter, peak 54 busy workers, 0% misses |
| Hedged requests at each call's p95, idempotent reads only | `call_dep`, `hedge_allowed` | Turn p99 10.24 s to 4.32 s for 4.19% extra calls. The model call and any write or side effect are never duplicated (tested) |
| Fallback ladder: primary, secondary provider, small local model, cached answer, honest error | `model_ladder` | Each rung must fit the leftover time. A write never steps down the ladder: it is done by the primary or refused honestly |
| Session store down: start a fresh session and tell the user; retrieval down: answer from model knowledge with a disclaimer | `load_session`, `answer_with_retrieval` | Tested; the turn is marked degraded |
| Deadline budget: session 0.5 s, parallel group 2.5 s, model 12 s, output moderation 1.5 s, reserve 3.5 s | `DeadlineBudget` | A step that overruns stops at its own slice; unused time and the reserve form a bank that only fallbacks may spend |
| Timeouts taken from the budget instead of a fixed 30 s | `DeadlineBudget.timeout_for` | Parallel group with one retry: 1.25 s per attempt. With breaker + bulkhead + these timeouts the brownout never filled the workers (peak 31 busy) and 0% of turns missed |

## Guardrails

- Never hedge or retry a call with side effects (refunds, account changes). Only idempotent reads are duplicated.
- Degraded answers say so: a fresh-session notice, a no-documents disclaimer, or the honest error.
- A slow step cannot borrow time from steps after it.
- The old pieces stay: `CircuitBreaker`, `RetryPolicy(timeout_s=4.0)`, `ModelGateway`'s honest 503, the job-queue bulkheads and the agent's `DEGRADED` reply are unchanged.

## Limitations

- Latencies are lognormal guesses, not traces from a real system; slow moderation calls are a 1% branch with a 4 s floor.
- Requests in the replay never queue at 400 a minute, so the 1% case shows tail growth but few misses. The brownout is a separate, deliberately harsh case.
- Retry backoff waits are left out; the breaker records an outcome when the call ends, but hedges and fallbacks are modelled as instant decisions.
- The fixed policy in the replay uses budget timeouts and hedging; the breaker and bulkheads are measured in the brownout.

## Built vs planned

| Piece | Status |
|---|---|
| Incident replay, fan-out formula, parallel proof, Little's-law brownout | Built, tested (simulated) |
| Slow-call breaker, per-dependency bulkheads, hedging, ladder, budget, derived timeouts | Built, tested (simulated) |
| Wiring `SlowCallBreaker` and `DeadlineBudget` into the live `ModelGateway` and `/v1/chat` path | Planned |
| Hedging against a real vendor with cancel-on-first-reply | Planned |

## Talk 17 additions: the mean hides the tail, and pages that do not wait

Simulated and seeded (`sysdesign/ext/tail_extras.py`, `docs/results/talk17.json`). The talk's own figures were a 90 ms mean, a 55 ms p50 and a 543 ms p99 (quoted). Ours come from 100,000 draws of a lognormal with a median of 55 ms.

| | mean | p50 | p90 | p99 |
|---|---|---|---|---|
| Simulated | 90.0 ms | 55.0 ms | 196.3 ms | 551.5 ms |

The mean looks healthy, yet 69% of requests are faster than it and the slowest 1% are 10x the median. Alert on percentiles, not on the mean.

**1% of requests is not 1% of users.** The formula from panel (a) above applies per user session too. With a 1% chance per request of a p99-slow call:

| Requests per session | 1 - 0.99^N | Simulated (4,000 users) |
|---|---|---|
| 1 | 1.0% | 0.9% |
| 10 | 9.6% | 10.0% |
| 50 | 39.5% | 40.3% |
| 100 | 63.4% | 63.9% |

**Partial page loads.** A page built from 100 parts that waits for all of them is as slow as its slowest part. `render_page()` shows what is ready at a 250 ms deadline and marks the rest "loading", to be filled in when they arrive. Over 2,000 simulated pages:

| | p50 page ready | p99 page ready |
|---|---|---|
| Wait for all 100 parts | 625.7 ms | 2,196.2 ms |
| Render ready parts at 250 ms | 250 ms | 250 ms |

The cost: 99.75% of pages had at least one part still loading at 250 ms, 6.28 parts on average and 12 at p99. That is fine for side panels. It is not acceptable for the part the user came for, which should get its own deadline.

Tests: `tests/test_talk17_tail_extras.py`.

Run it: `python scripts/tail_latency_bench.py && python scripts/tail_evidence_board.py`; tests in `tests/test_talk16_tail_latency.py`.
