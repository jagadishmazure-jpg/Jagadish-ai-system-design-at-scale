# Design stack (layer → module)

| Layer | Module |
|---|---|
| Edge | `deploy/nginx`, `sysdesign/gateway.py`, `sysdesign/auth.py` |
| Admission | `sysdesign/admission.py` (Redis Lua) |
| Cache / route / resilience | `cache.py`, `router.py`, `resilience.py`, `llm.py` |
| Agent | `agent.py`, `tools.py`, `approvals.py`, `guardrails.py`, `grounding.py`, `policy.py`, `structured.py` |
| Knowledge | `rag.py`, `knowledge.py`, `context.py`, `memory.py` |
| Work | `jobqueue.py`, `autoscaler.py`, `workflows.py`, `runtime.py`, `distributed.py` |
| Multi-agent / MCP | `multiagent.py`, `mcp.py` |
| Data | `store.py`, `backend.py`, `events.py` |
| Quality | `evals.py`, `mlops.py`, `release.py`, `observability.py` |
| Governance | `governance.py`, `pii.py`, `enterprise.py` |
| Capacity | `capacity.py`, `calc.py` |
| Delivery | `Dockerfile`, `k8s/`, `terraform/`, `infra/`, `deploy/`, `.github/workflows/` |
