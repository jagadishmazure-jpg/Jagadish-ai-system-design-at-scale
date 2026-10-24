# Interview points

Short answers, each backed by code or a number in this repo.

- **"Why did the agent fall over at 10k users?"** It was usually the system
  around the model: no admission control, synchronous long jobs, a single
  provider, and state in memory. This repo fixes each one and measures it.
- **Rate limit vs token budget vs concurrency cap.** These protect fairness,
  money and provider quota respectively. One noisy user should get 429s while
  everyone else stays fast (`rate_limit` scenario).
- **API gateway vs model gateway.** See `docs/api-gateway-vs-model-gateway.md`.
- **What do you cache?** Only knowledge answers, keyed by tenant and
  permissions, with source tracking for CDC invalidation. Never cache prices,
  orders, actions or PII. The design target is zero wrong-source hits.
- **How do you cut cost?** A router ladder: rules, then a classifier, then
  small, then large. The README shows the cost per 1k requests against
  all-large, and the capacity calculator shows the monthly figure.
- **Retry storms.** Use bounded retries with full jitter, then a breaker so a
  dead provider stops receiving traffic, then fallback. The `outage` scenario
  shows calls to the failing primary dropping.
- **A pod dies mid-refund.** The workflow resumes from its checkpoint, and
  because the refund is idempotent it is issued exactly once.
- **Why not autoscale on CPU?** LLM workers mostly wait on I/O. Queue depth and
  the age of the oldest job are what users feel.
- **What would you do next?** OTLP export to a real trace backend, a trained
  injection classifier, a vector index for the cache, real provider adapters,
  and a k6 run against compose.
- **Honesty point.** The numbers come from a deterministic model, not
  production. Say so first.
