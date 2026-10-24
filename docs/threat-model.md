# Threat model

Scope: the API, admission, guardrails, tools, queue and workflow. Assets:
customer PII, order data, refund money, model spend and policy content.

## STRIDE

| Threat | Example | Control | Test |
| --- | --- | --- | --- |
| Spoofing | forged or `alg=none` JWT, a token for another audience | RS256 only, pinned issuer/aud/exp, kid lookup | `tests/test_auth.py` |
| Tampering | editing policy docs to poison answers | `policy:write` scope; CDC invalidates dependent cache entries | `test_admin_requires_scope`, `tests/test_cache_cdc.py` |
| Repudiation | refunds without a trail | request ids, workflow checkpoints per step, metrics | `tests/test_queue_workflow.py` |
| Information disclosure | reading another user's order; PII in traces or cache | ownership check with uniform NotFound; redaction at input, output, handoff and trace export; PII questions never cached; cache namespaced by tenant and permissions | `tests/test_agent.py`, `tests/test_pii.py` |
| Denial of service | one user flooding; huge bodies; token-heavy prompts | nginx and in-app body caps, token bucket, token budgets, model-call cap, bounded queues | `tests/test_admission.py`, loadtest `rate_limit` |
| Spoofing (partner) | a leaked or guessed partner webhook key | API keys stored only as a PBKDF2 digest, scoped, expiring and revocable; a customer JWT is not accepted on the webhook route | `tests/test_talks_auth.py` |
| Information disclosure (push) | subscribing to another user's order events | SSE route checks ownership and returns the same 404 for missing and not-yours | `test_order_events_ownership` |
| Denial of service (fairness) | one user's parallel calls holding every model slot | per-user share inside the cluster model-call cap | `test_one_user_cannot_take_every_model_slot` |
| Elevation of privilege | prompt injection asking for a full refund or big credit | rules + classifier; actions authorized in code, not by the model | `tests/test_guardrails.py` |

## OWASP Top 10 for LLM applications (2025), mapped

- **LLM01 Prompt injection:** input rules on normalised text plus a
  classifier, with tools authorized in code. Residual risk: novel phrasings.
- **LLM02 Sensitive information disclosure:** PII redaction everywhere text
  leaves the request, and the cache refuses PII.
- **LLM05 Improper output handling:** output guardrails block over-policy
  credits, promises and model-stated prices.
- **LLM06 Excessive agency:** refunds are capped by the order total, credits by
  policy and once per order, and cancellations and disputes go to humans.
- **LLM08 Vector and embedding weaknesses:** the cache is keyed by tenant and
  permissions, and entries are invalidated by source chunk.
- **LLM10 Unbounded consumption:** token budgets, the concurrency cap,
  bounded retries and breakers.

Not covered: supply-chain review of model weights (no real models), and
training-data poisoning (no training).
