# Agent boundaries

| The agent may | The agent may not |
|---|---|
| Read policies and the caller's own orders | Read another customer's order (`test_ben_cannot_see_ana_order`) |
| Propose a refund | Execute a refund without owner confirmation and a digest-matched approval |
| Call allow-listed tools with schema-checked args | Approve its own request, or pass extra fields |
| Say "I don't know" | Invent a number or a policy (grounding and citation checks) |
| Hand billing disputes to a human | Settle disputes |
