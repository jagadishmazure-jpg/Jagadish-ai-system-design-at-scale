# ADR 0002: Redis Lua scripts for limits, budgets and the model-call cap

**Status:** accepted

**Context:** Replicas are stateless. Limits held in process memory multiply by
the number of replicas, and a read followed by a separate write races.

**Decision:** Each check is one Lua script, which runs atomically in Redis.
Concurrency slots are leases with an expiry, so a crashed replica cannot leak
them forever.

**Consequences:** Redis becomes a dependency on the hot path. In this design, if
Redis is down, admission fails closed. Tests use fakeredis[lua], and CI also
runs the scripts against a real Redis service container.
