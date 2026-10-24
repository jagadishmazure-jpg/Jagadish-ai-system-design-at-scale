# RAG pipeline: decisions backed by numbers

All figures are from `python -m loadtest.benchmarks` over a small, self-written policy corpus with 16 questions. Treat them as regression checks, not quality claims.

**Chunk size** (top-1 chunk contains the fact):
| Words / overlap | Chunks | Top-1 has fact | Avg tokens returned |
|---|---|---|---|
| 8 / 4 | 43 | 0.3125 | 11.1 |
| 16 / 8 | 22 | 0.6875 | 20.9 |
| 32 / 16 | 11 | 0.6875 | 43.1 |
| 64 / 32 | 6 | 0.75 | 76.7 |
16 words gives most of the accuracy at roughly a quarter of the tokens of 64 words. Very small chunks cut facts in half.

**Reranker.** hit@1 is 0.875 both with and without it, and it adds 4.26 ms p50. The rule is to keep it only if hit@1 improves by at least 0.05 at no more than 10 ms extra, so `keep_reranker = false` on this corpus. A larger or noisier corpus could flip this decision. The rule is the asset, not the verdict.

**Context funnel.** 13,055 raw tokens are reduced to 25 decision-relevant tokens for a refund-eligibility check, and a 429-token conversation is compacted to 42.

**Safety.** Poisoned documents are isolated (`test_poisoned_doc_is_isolated`). Answers must cite a supporting chunk (`test_citation_rejects_unsupported_claim`). Counting questions go to SQL, not vectors (`test_vector_alone_is_wrong_for_counting`).
