# Load model

`loadtest/harness.py` runs six scenarios. Each one replays identical seeded
traffic twice, once with the pattern off and once with it on. The scenarios
use the real Redis scripts (fakeredis), router, cache, gateway, queue and
autoscaler. Time is virtual (`FakeClock`), so a run takes seconds and gives the
same numbers on every machine. CI reruns it with `--check`.

What it is: a deterministic model of queueing and failure behaviour, used to
compare designs. What it is not: a benchmark of hardware, network or real
provider latency. Scenario sizes are 1,000 to 4,000 requests.

| Scenario | Off | On | Measures |
| --- | --- | --- | --- |
| rate_limit | no limiter | per-user token bucket | normal users' p95 and errors while one user floods |
| cache | cache disabled | semantic cache | hit rate, cost per 1k, wrong-source hits |
| router | everything to the large model | ladder | cost per 1k and per rung |
| outage | retry against the primary only | breaker + fallback | errors and primary calls during a 60-second outage (of 180 s) |
| replica_failure | one replica, no failover | 3 replicas, health-checked | error rate when a replica dies |
| queue | one shared FIFO, fixed workers | priority pools + autoscaler | interactive p95 wait, bulk drain time |

Percentiles use the nearest-rank method over successful responses. An empty set
gives NaN, never 0.
