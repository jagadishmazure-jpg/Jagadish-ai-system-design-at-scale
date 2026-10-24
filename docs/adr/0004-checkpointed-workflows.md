# ADR 0004: checkpoint multi-step workflows instead of sticky sessions

**Status:** accepted

**Context:** A refund has several steps: validate, issue, notify. If the
replica running it dies and the state lived in memory, the job is either lost
or repeated.

**Decision:** Write a checkpoint after every step, in SQLite for tests and
Postgres in compose. The money-moving step uses an idempotency key derived
from the workflow id. Queue leases expire, so another worker picks the job up
and resumes it.

**Consequences:** Every step costs an extra write. The chaos test kills a
workflow mid-way and asserts the refund was issued exactly once.
