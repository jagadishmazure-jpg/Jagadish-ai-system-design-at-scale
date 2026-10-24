# Implementation guide

How to take one pattern from this repo into your own service. Each section
names the module, the decision that matters and the test that proves it.

## 1. Gateway (`sysdesign/app.py`, `deploy/nginx.conf`)
Give the system one entrance. Reject cheap failures first: body size, then
route, then auth. Validate incoming request ids against a safe pattern, and
replace them otherwise, so a hostile id cannot reach your logs. Health
(`/healthz`) and readiness (`/readyz`, which pings Redis) are separate, so the
load balancer stops sending traffic to a replica that has lost its dependencies.

## 2. Admission control (`sysdesign/admission.py`)
Make three separate decisions: request rate (token bucket), spend (token budgets
with reserve and settle), and concurrency (leased slots). Keep each one as a single
Lua script. Always return `Retry-After`, computed from the bucket refill time or
the start of the next budget window. Reject negative estimates, because a
negative reservation would add budget back.

## 3. Guardrails (`sysdesign/guardrails.py`, `sysdesign/pii.py`)
Normalise text before matching, by applying NFKC and stripping zero-width
characters. Layer cheap rules, then a classifier. Put action authorization in
code that the model cannot talk its way past. Use a Luhn check on card
candidates so order numbers aren't redacted as cards.

## 4. Router and HITL (`sysdesign/router.py`)
Order matters. Check human-only intents first, then deterministic rules, then
the classifier. Only escalate to bigger models when the cheaper rung is not
enough. Measure cost per rung (`PATHS` table in the README).

## 5. Semantic cache and CDC (`sysdesign/cache.py`, `sysdesign/knowledge.py`)
Decide what may be cached before you choose the similarity threshold. Key by
tenant and permissions. Store source chunk ids with every entry so a document
change drops only what it touched.

## 6. Queue and bulkheads (`sysdesign/jobqueue.py`)
Bound every queue, and give interactive and bulk work separate pools so a
backlog in one can't starve the other. Apply max wait only to jobs never
started. A resumed job must not be expired. There is a regression test for
this, because it was a real bug here.

## 7. Resilience (`sysdesign/resilience.py`)
Set a timeout on every attempt, cap retries at two with full jitter, give each
provider its own breaker, and have a fallback. When everything fails, say so
plainly and refund the reserved tokens.

## 8. Stateless replicas and checkpoints (`sysdesign/checkpoints.py`, `sysdesign/workflow.py`)
Put no state in process memory that you can't lose. Checkpoint after each
step, and make side effects idempotent. Run the chaos test
(`tests/test_queue_workflow.py`) after any change.

## 9. Autoscaler (`sysdesign/autoscaler.py`)
Scale on what users feel: queue depth per worker and the age of the oldest job.
Keep a warm minimum, account for start-up delay, and use a cooldown so it
doesn't flap.

## 10. Streaming (`/v1/chat/stream`)
Send SSE with `X-Accel-Buffering: no` and turn off nginx buffering. Record
time to first event as its own metric, because it is what users perceive.

## 11. Observability (`sysdesign/observability.py`, `deploy/alerts.yml`)
Redact in the exporter, not at call sites. That way, a forgotten call site
still can't leak. SLO checks need a minimum sample count. Functional evals
assert the right tools ran for each intent.

## 12. Capacity (`sysdesign/capacity.py`)
Run the numbers first. Then change the peak multiplier and the cache hit rate to
see which assumption your design depends on most.
