# Coverage map: 14 sources → code → proof

Sources are 14 talks on system design. They are credited by topic only and paraphrased in my own words. Nothing is quoted.

| # | Source topic | Where in this repo | Proof |
|---|---|---|---|
| 1 | Scaling an AI app: the 12 base patterns (gateway, admission, cache, router, resilience, queue, autoscale, RAG, evals, observability, cost, security) | `sysdesign/*` | tests across all files; `loadtest/harness.py` |
| 2 | Agent design: tools, approvals, guardrails | `agent.py`, `tools.py`, `approvals.py`, `guardrails.py` | `test_agent_paths.py`, `test_approvals.py` |
| 3 | What "agentic" really means | WHAT_IS_AGENTIC.md | `test_greeting_uses_no_model` |
| 4 | Latency budgets and streaming | LATENCY.md, SSE | stage timings, `test_live_order_push_over_sse` |
| 5 | Capacity and cost estimation | `capacity.py`, CAPACITY.md | `test_per_turn_vs_per_conversation_mistake_and_solver` |
| 6 | RAG pipeline choices | `rag.py`, RAG_PIPELINE.md | chunk-size and reranker benchmarks |
| 7 | Enterprise: governance, privacy, FinOps | `governance.py`, `pii.py`, ENTERPRISE.md | residency and forgetting tests |
| 8 | Evals, drift and the learning loop | `evals.py`, `mlops.py` | `test_evals_release.py` |
| 9 | Multi-agent and MCP | `multiagent.py`, `mcp.py` | discovery and ownership tests |
| 10 | Distributed systems: CAP, sticky routing, sagas | `distributed.py`, `workflows.py` | CHWBL 0.388 → 0.80, saga test |
| 11 | Networking from DNS to HTTP | NETWORKING_JOURNEY.md, `netcheck.py` | `test_network_tools.py` |
| 12 | Backend evolution | BACKEND_EVOLUTION.md, `backend.py` | atomicity, index, object store, restart tests |
| 13 | DevOps journey | DEVOPS_JOURNEY.md, `k8s/`, `terraform/`, `deploy/` | kind rollout (CI), `test_deploy.py` |
| 14 | Authentication terms | AUTHENTICATION.md, `auth.py` | `test_gateway_auth.py`, `test_real_redis.py` |
