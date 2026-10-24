# Worked interview cases

Seven fictional business cases, each answered with the [playbook](../interview-playbook.md) method. Numbers come from `CASES` in `sysdesign/ext/envelope.py` (illustrative prices); `scripts/render_interview.py --check` fails CI if a table drifts.

| # | Case | What makes it different |
|---|---|---|
| 1 | [Bank assistant (Northwind Ledger Bank)](01-bank-assistant.md) | Write actions, human gates, financial-advice guardrail, residency |
| 2 | [Retail support (Brightcart)](02-retail-support.md) | Volume, caching, order lookups; links to talks 15-16 |
| 3 | [Insurance claims extraction (Harborline Mutual)](03-insurance-claims.md) | Batch documents, schema output, review queue |
| 4 | [Healthcare intake (Cedarbrook Clinics)](04-healthcare-intake.md) | Safety first, abstain, escalate, never diagnose |
| 5 | [Mortgage document Q&A (Keystone Home Lending)](05-mortgage-qa.md) | Long documents, citations, per-application isolation |
| 6 | [Internal knowledge search (Fernwave Systems)](06-enterprise-search.md) | Permissions-aware retrieval, spiky load |
| 7 | [Low-latency checkout risk notes (Quickpay Lane)](07-low-latency.md) | Tens of milliseconds, tiny prompts, no LLM on the hot path |

<!-- envelope:prices -->
| Tier | Input $/1M | Output $/1M |
|---|---|---|
| small | 0.15 | 0.60 |
| mid | 1.00 | 4.00 |
| frontier | 5.00 | 20.00 |
<!-- /envelope:prices -->
