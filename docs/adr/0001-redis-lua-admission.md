# ADR 0001: Admission control in Redis Lua
**Status:** accepted. **Context:** limits must hold across replicas. **Decision:** token buckets, budgets and concurrency leases are atomic Lua scripts in Redis. **Consequences:** one network hop per check (about 0.77 ms for gateway plus admission, simulated). A Redis outage needs a fail-closed policy. The tenant cap is not per-user fair (see PITFALLS).
