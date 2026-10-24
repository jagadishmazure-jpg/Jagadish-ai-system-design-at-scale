# Talk 17 add-on: retries that cannot pile up

A simulation in virtual time with seeded failures (`sysdesign/ext/retries.py`, numbers in `docs/results/talk17.json`). It reuses the existing `RetryPolicy` (max 2 retries) and the job queue's idempotency ledger. Source: a related public talk, credited by topic only and paraphrased.

## Retry budget

`RetryBudget(ratio=0.10, window_s=60)` lets retries use at most 10% of the requests seen in the last minute. At 400 requests a minute that is 40 retries. Once it is spent, a failing call is not retried. It fails fast with 503 (`http_status`). Negative ratios and windows are rejected.

## Amplification: calls that reach the store per click (4,000 clicks, three layers)

| Store failure rate | One layer retries | One layer + budget | Every layer retries | Every layer + budget |
|---|---|---|---|---|
| 5% | 1.045 | 1.045 | 1.045 | 1.045 |
| 20% | 1.226 | 1.098 | 1.235 | 1.233 |
| 100% (outage) | 3.0 | **1.1** | **27.0** | 1.329 |

- During an outage, retrying in every layer multiplies: 3 x 3 x 3 = 27 calls per click against a store that is already down.
- Retrying in one layer only gives 3x. Adding the budget brings it to 1.1x. The talk also says about 1.1x (quoted), and our measured value matches.
- Budgets in every layer still compound to 1.33x. Retry in one place.
- The cost is real. At a 20% failure rate the budget lets fewer clicks succeed (89.3% vs 99.3%), because 10% of volume is less than the retries needed. Pick the ratio with that in mind.

## A second click while the first is in flight

`double_click()` sends two refund clicks with the same idempotency key, 10 ms apart, while the first refund takes 50 ms. The second click finds the existing job through the ledger (`JobQueue.enqueue` returns the same job id), cannot claim it, and waits for `done`. It then reads the one stored result. Measured in tests: the same job id, the same refund id, `issue_refund` called once, and one refund issued. A late click, after the first one finished, reads the stored result straight away.

## Built vs planned

| Piece | Status |
|---|---|
| `RetryBudget`, `call_with_budget`, fail-fast 503 | Built, tested (simulated) |
| In-flight duplicate wait on the job ledger | Built, tested (fakeredis) |
| Amplification study | Built, tested (simulated) |
| Budget wired into `ModelGateway` and the live `/v1/chat` path | Planned |

Tests: `tests/test_talk17_retries.py`.
