# Coverage map: 14 talks → code → proof

The sources are 14 public talks on system design. They are credited by topic only and paraphrased in my own words; nothing is quoted. Talks 1 and 2 shaped main's base build. Talks 3 to 14 were added later from the `grokbot/full-build` branch and live mostly in `sysdesign/ext/` so they reuse main's gateway, auth, PII, guardrails and admission instead of duplicating them.

Status key: **Built** = code plus tests in CI. **Built, real infra** = also exercised on real infrastructure in CI. **Written, not run** = config exists but nobody has run it. **Planned** = not built.

| # | Talk topic | Where in this repo | Proof | Status |
|---|---|---|---|---|
| 1 | Scaling an AI app: gateway, admission, cache, router, resilience, queue, autoscale, observability, cost | `sysdesign/*` (main) | main's tests, `loadtest/harness.py` | Built |
| 2 | Agent design: tools, guardrails, human handoff | `agent.py`, `tools.py`, `guardrails.py`, `pii.py` | `test_agent.py`, `test_guardrails.py` | Built |
| 3 | What "agentic" means | [what-is-agentic.md](what-is-agentic.md), `ext/grounding.py`, `ext/context.py` | `test_grounding_blocks_invented_number`, `test_step_cap_from_budget` | Built |
| 4 | Latency budgets, batching, streaming | [latency.md](latency.md), `ext/distributed.py`, SSE order push | `talks_benchmarks.json` batching table, `test_live_order_push_over_sse` | Built (simulated GPU) |
| 5 | Capacity and cost estimation at 10M users | [cost-model-10m.md](cost-model-10m.md), `ext/cost_model.py`, `scripts/estimate_10m.py` | `test_per_turn_vs_per_conversation_mistake_and_solver` | Built (illustrative prices) |
| 6 | RAG pipeline choices | [rag-pipeline.md](rag-pipeline.md), `ext/rag.py`, `ext/knowledge_router.py` | chunk-size and reranker benchmarks, `test_vector_alone_is_wrong_for_counting` | Built |
| 7 | Enterprise: governance, privacy, FinOps | [enterprise.md](enterprise.md), `ext/governance.py`, `ext/enterprise.py` | residency, forgetting, FinOps tests | Built |
| 8 | Evals, drift, MLOps hygiene | `ext/mlops.py` | `test_feature_skew_registry_and_psi`, `test_version_pin_rolls_back`, `test_canary_shadow_ab` | Built. The branch's own eval suite was not ported (it depends on the branch agent); main keeps `test_functional_eval` |
| 9 | Multi-agent and MCP | `ext/multiagent.py`, `ext/mcp.py` | discovery and ownership tests | Built |
| 10 | Distributed systems: CAP, sticky routing | `ext/distributed.py` | CHWBL hit rate 0.388 → 0.80 (max load 58 vs avg 50), CAP test | Built |
| 11 | Networking from DNS to HTTP | [networking-journey.md](networking-journey.md), `scripts/netcheck.py`, `tools/latency_probe.py` | `test_talks_network.py` | Built. Compose network segmentation is Planned |
| 12 | Backend evolution | [backend-evolution.md](backend-evolution.md), `ext/backend.py`, partner webhook | atomic refund, index proof, object store, restart tests | Built |
| 13 | DevOps journey | [devops-journey.md](devops-journey.md), `k8s/`, `terraform/`, `infra/`, `deploy/` | kind rollout job, `test_talks_deploy.py`, `iac-validate` job | Built, real infra (kind). Terraform/Bicep validated only, never applied. Argo CD Planned |
| 14 | Authentication by caller type | [authentication.md](authentication.md), `ext/authn.py`, gateway API-key path | `test_talks_auth.py`, `test_talks_real_redis.py` (real Redis in CI) | Built |

Not ported on purpose: the branch's own agent, cache, router, admission, resilience, queue, autoscaler and workflow modules (main already has them), and the branch's digest-bound approval flow (main uses a human handoff instead).
