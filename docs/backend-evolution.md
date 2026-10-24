# Backend evolution: from one script to a platform

Each stage exists because the stage before it hit a specific wall. The code is in `sysdesign/` (main) and `sysdesign/ext/backend.py`; the proof is in `tests/test_talks_platform.py` and main's tests.

| Stage | Shape | Wall it hits | What fixes it (here) |
|---|---|---|---|
| 0 | One process, state in Python dicts | A restart wipes every order (`test_in_memory_state_lost_on_restart_db_survives`) | Stage 1 |
| 1 | Process + database (sqlite here, Postgres in compose) | Partial writes: the refund is paid but never recorded | Transactions: `refund_with_record` commits both rows or neither (`test_refund_and_record_atomic`) |
| 2 | Indexes | A lookup by customer scans all 200k rows | `idx_orders_customer`: the plan changes from SCAN to SEARCH, and a lookup drops from 6.83 ms to 0.0052 ms (`test_index_changes_plan_and_speeds_lookup`) |
| 3 | Object storage | Attachment bytes bloat the DB | MinIO (compose) / `LocalObjectStore`. The DB stores a link only, and signed URLs expire (`test_attachment_bytes_in_object_store_only_link_in_db`). A CDN in front is **planned** |
| 4 | Stateless replicas behind nginx | Sessions and rate limits live in one process's memory | Redis for sessions, token buckets and leases (`test_limits_shared_across_replicas`) |
| 5 | Cache | Repeat questions pay for the model every time | A tenant-partitioned semantic cache |
| 6 | Queue + workers | Slow jobs block requests | A priority queue with idempotency, a bound and a DLQ, plus an autoscaler on depth and age |
| 7 | Push, not poll | Clients hammer `GET /orders/{id}` | SSE `/v1/orders/{id}/events` with an ownership check, fed by `OrderEvents` (`test_live_order_push_over_sse`) |
| 8 | Readiness, health and versions | A broken pod still gets traffic | `/readyz` returns 503 on broken config; `/version` reports the git SHA. See [devops-journey.md](devops-journey.md) |

## Step 4 in detail: where session state lives (talk 17)

Simulated replay (`sysdesign/ext/stateless.py`, `docs/results/talk17.json`): 3 replicas, 60 users over 12 rounds, requests arriving in random order. Each user logs in, then adds cart items or fills form sections.

| Setup | Logouts | Lost cart items (of 397) | Lost form sections | One replica dies mid-session: logouts / lost items / lost sections | Heavy user at 10x traffic: max/avg load |
|---|---|---|---|---|---|
| Memory only, round robin | 118 | 195 | 136 | 114 / 184 / 127 | 1.0 |
| Sticky sessions, memory | 0 | 0 | 0 | 29 / 83 / 62 | **1.846** |
| Shared store (Redis), round robin | 0 | 0 | 0 | **0 / 0 / 0** | 1.0 |

Sticky sessions look fine until a replica dies. Then everyone pinned to it loses their session. They also pile a heavy user onto one box. A shared store makes every replica interchangeable.

**Counter demo.** Nine hits on a page counter: memory-only replicas each answer 1, 2, 3 (three counters of 3), and the shared store answers 9.

**Scaling up vs scaling out.** A bigger box and several smaller boxes with the same total capacity cost roughly the same. Scaling out also gives redundancy: losing one of three boxes costs a third of capacity instead of all of it. It works only once state lives outside the process, which is what this step is for. No prices are claimed here.

**Real fix in this repo.** `Commerce` (orders, prices, refunds, credits and the new carts) now takes a Redis client. `build_app` and the worker both pass the shared Redis, so every API replica and worker sees the same data, and refund ids come from one Redis counter. Without Redis it falls back to plain dicts for in-process tests. `test_two_replicas_see_the_same_cart` and `test_two_app_replicas_share_commerce` prove it.

## Trace: "place order"
1. **Client → nginx.** TLS 1.3 terminates. Bodies over 16k are rejected at the edge.
2. **Gateway.** The bearer JWT is verified locally (no DB). A request id is attached. The route needs the `orders:write` scope.
3. **Admission.** The user bucket, then the tenant bucket, then the budget. A 429 names its reason.
4. **Handler.** One DB transaction inserts the order and its line items. On any failure, neither is written.
5. **Attachment, if any.** The bytes go to object storage, and the order row stores the key.
6. **Event.** `OrderEvents.publish` pushes `order.created` to SSE subscribers that own the order.
7. **Async work.** A queue job sends the confirmation. It is idempotent on the order id, so a retry does not send twice.
8. **Partner update.** A webhook with an API key (`apikey:webhooks:write`) updates the status, and SSE pushes it to the customer.
9. **Telemetry.** `requests_total{path,status}` and `request_latency_seconds` are recorded. A 5xx spike feeds the 1% alert.
