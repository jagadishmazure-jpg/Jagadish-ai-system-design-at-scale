"""Task scope as code: the authority matrix and the workflow ownership table.

These tables are imported by the pipeline (approvals.py, agent.py) and checked by tests, so the
documented scope in docs/requirements.md cannot silently drift from what the code enforces.
"""
from __future__ import annotations

AI_REFUND_LIMIT_USD = 100.00   # standard-tier limit; premium is 250 (policy.ENTITLEMENTS). Above it: human

# decision -> who makes it. "agent" = the AI pipeline may decide (via deterministic checks where noted),
# "human" = the AI may only collect details and hand off.
AUTHORITY = {
    "answer_policy_question": "agent",
    "report_order_status": "agent",
    "request_missing_info": "agent",
    "refund_within_policy_up_to_limit": "agent",      # after deterministic checks + user confirmation
    "refund_above_limit": "human",
    "refund_outside_policy_exception": "human",
    "grant_discount_or_credit": "human",
    "cancel_order": "human",                          # AI files a confirmed request, a person executes it
    "billing_dispute": "human",
    "account_closure": "human",
}

# Each workflow step has exactly one owner, with the handoff it produces.
# owner: "code" (deterministic), "router" (intent router/classifier), "model" (LLM).
WORKFLOW = [
    {"step": "verify_identity",        "owner": "code",   "handoff_to": "screen_input"},
    {"step": "screen_input",           "owner": "code",   "handoff_to": "classify_intent"},
    {"step": "classify_intent",        "owner": "router", "handoff_to": "assess_risk"},
    {"step": "assess_risk",            "owner": "code",   "handoff_to": "retrieve_order_history"},
    {"step": "retrieve_order_history", "owner": "code",   "handoff_to": "inspect_policy"},
    {"step": "inspect_policy",         "owner": "code",   "handoff_to": "request_missing_info"},
    {"step": "request_missing_info",   "owner": "code",   "handoff_to": "draft_answer"},
    {"step": "extract_action_fields",  "owner": "model",  "handoff_to": "inspect_policy"},
    {"step": "draft_answer",           "owner": "model",  "handoff_to": "screen_output"},
    {"step": "route_or_escalate",      "owner": "router", "handoff_to": "human_queue"},
    {"step": "approve_high_risk",      "owner": "code",   "handoff_to": "user_confirmation"},
    {"step": "screen_output",          "owner": "code",   "handoff_to": "respond"},
]


def owner_of(step: str) -> str:
    owners = [w["owner"] for w in WORKFLOW if w["step"] == step]
    if len(owners) != 1:
        raise KeyError(f"{step} has {len(owners)} owners")
    return owners[0]


def decides(decision: str) -> str:
    return AUTHORITY[decision]
