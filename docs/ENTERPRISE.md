# Enterprise triad: governance, privacy, cost

- **Governance.** Data residency never leaves the region (`test_residency_never_leaves_region`). Tool contracts reject extra fields. Every risky tool has one owner, and nobody approves their own request.
- **Privacy.** PII is redacted from traces, cache and feedback. Third parties never get raw PII. Right-to-be-forgotten and minimum-necessary are tested.
- **Cost (FinOps).** Budgets are reserved *before* spend. A negative estimate is rejected, because it would refund budget. Cost per successful task is $4.06e-06 on the eval suite (fake prices).
