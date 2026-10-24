# ADR (talk 18): dated retrieval before a verifier; CPU work in a process pool

**Status:** accepted for the simulated replay. Not wired into the live gateway yet.

**Context.** Stale answers came from an index holding every policy version. CPU-heavy work on the event loop froze unrelated chats.

**Decision.** (1) Filter retrieval by effective date first, then verify answers, and send low-confidence answers to people. A verifier alone caught everything but sent 78% of answers to review in the replay. (2) Run CPU-bound steps in a `ProcessPoolExecutor`, not on the loop and not in threads.

**Consequences.** Policy documents must carry dates. The review queue needs staffing. Process pools add start-up time and need picklable arguments.
