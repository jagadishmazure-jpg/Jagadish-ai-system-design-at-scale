# Case 4: Cedarbrook Clinics patient intake assistant

**Prompt:** "Design an assistant that helps patients before a clinic visit." (Fictional clinic network.)

## Clarify (assumed answers)

| Question | Assumed answer | Drives |
|---|---|---|
| Users and frequency? | 300k patients, 30% use it on a given day, seven turns | Moderate scale; caching only for admin FAQs |
| How fast? | Conversational, a second or two | Streaming |
| Freshness? | Clinic hours and forms weekly; clinical protocols reviewed quarterly | RAG over approved content only |
| Cost of an error? | Very high: a missed emergency | Red-flag rules before any model; abstain; never diagnose |
| Budget? | $0.03 per conversation | Allows a 15% frontier floor for symptom summaries |

## Envelope

<!-- envelope:case:healthcare -->
| Quantity | Value |
|---|---|
| Conversations per day | 90k |
| Model requests per day | 630k |
| Average QPS (requests / 86,400) | 7.3 |
| Peak QPS (x3) | 21.9 |
| Tokens per request (system + context + output) | 800 + 1,200 + 250 = 2,250 |
| Input tokens per day | 1.26B |
| Output tokens per day | 157.5M |
| Tokens per day | 1.42B |
| Cost per day at 75% small / 20% mid / 5% frontier | $1,063 |
| Cost per day if everything went to frontier | $9,450 |
| Cost per conversation | $0.0118 |
| Target per conversation | $0.03 |
| Meets target at the starting mix? | yes |
| Best-quality mix that fits (frontier >= 15%) | 25% small / 60% mid / 15% frontier = $0.0291 |
| Cheapest feasible mix | 85% small / 0% mid / 15% frontier = $0.0184 |
| 10x users: peak QPS | 219 |
| 10x users with 30% cache hits: model peak QPS | 153 |
| 10x users with 30% cache hits: cost per day | $7,442 |
<!-- /envelope:case:healthcare -->

## High-level design

```mermaid
flowchart TD
  P[Patient] --> G[Gateway + consent check]
  G --> RF{Red-flag rules<br/>chest pain, breathing, self-harm}
  RF -->|match| EM[Show emergency instructions + alert nurse]
  RF -->|no match| R{Router}
  R -->|admin FAQ| S[RAG over approved content + small]
  R -->|symptom intake| F[Structured questionnaire + frontier summary]
  F --> CF{Confidence and scope check}
  CF -->|out of scope or unsure| AB[Abstain: "a nurse will contact you"]
  CF -->|ok| N[Summary to nurse, not to patient as advice]
  S --> OG[Output guard: no diagnosis, no dosing]
  OG --> P
```

## Deep dive

Deterministic red-flag rules run before the model, so a model outage can never hide an emergency. The model writes a structured summary for clinicians; the patient sees only questions and logistics. Abstention is a designed outcome with its own metric, not a failure.

## Trade-offs

Higher abstention means more nurse calls; lower abstention means more risk. Measure both on a clinician-labelled golden set and set the threshold with the clinical lead.

## Failure modes

Patient describes an emergency in casual language the rules miss (fix: model-based red-flag classifier as a second net, tuned for recall); PHI in logs (mask before writing, `sysdesign/pii.py`).

## 10x follow-up

Flu season. The rules layer is cheap and scales; nurse capacity does not, so the real 10x constraint is the human queue. Prioritise by red-flag score.

## Wrap-up

Raise: consent and retention rules, and who signs off on prompt changes (clinical governance, versioned with the model).
