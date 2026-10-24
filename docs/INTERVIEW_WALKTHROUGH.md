# Interview walkthrough (about 10 minutes)

1. **Requirements (1 min).** Support agent for an online store: under $0.01 a conversation, p95 under 2.5 s, zero PII leaks, zero-downtime deploys. See requirements.md.
2. **Estimate (1 min).** 10M users × 5 requests a day gives 579 avg / 1,736 peak QPS. A turn costs $0.003375, but a 4-turn conversation is $0.0135, over budget. The solver's mix of 95% small and 5% frontier gives $0.00942.
3. **High-level design (2 min).** Use the README diagram: edge → gateway → admission → cache → router → resilience → providers, with the agent beside it and a queue for slow work.
4. **Deep dive 1: overload (2 min).** Redis Lua buckets, budgets and leases. The noisy-neighbour numbers, and the honest fairness gap.
5. **Deep dive 2: correctness (2 min).** Approvals, grounding, side-effect verification, the eval gate.
6. **Operations (2 min).** Image tagged with the SHA, maxUnavailable 0 with readiness, the kind proof, the 1% alert → git revert.
