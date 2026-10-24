# ADR 0003: cache only knowledge answers

**Status:** accepted

**Context:** Semantic caches save money, but a near-match on "what's my order
status" or a price gives a wrong answer that looks correct.

**Decision:** Only the policy intent is cacheable. Never cache tool results,
prices, actions or any question containing PII. Namespace the cache by tenant
and permission set, set a TTL, and record source chunks so CDC can drop
exactly the affected entries.

**Consequences:** The hit rate is lower than caching everything would give. In
exchange, the load model records zero wrong-source hits.
