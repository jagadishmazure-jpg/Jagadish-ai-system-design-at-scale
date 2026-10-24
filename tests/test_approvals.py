from sysdesign.tools import ORDERS
from tests.conftest import ANA, run

APPROVER = {"sub": "lead-1", "tenant": "quillmoor", "scope": "approver"}


def test_high_risk_tool_refuses_without_approval(system):
    r = run(system.registry.call("issue_refund", {"order_id": "QM-10001", "amount": 59.99, "reason": "damaged"}, ANA))
    assert not r.ok and system.store.effects("refund") == []


def test_confirm_digest_must_match_and_executes_once(system):
    p = system.gate.propose("w1", "issue_refund", {"order_id": "QM-10001", "amount": 59.99, "reason": "damaged"}, ANA)
    assert run(system.gate.confirm(p["proposal_id"], "0" * len(p["confirm_digest"]), ANA))["status"] == "rejected"
    assert run(system.gate.confirm(p["proposal_id"], p["confirm_digest"], ANA))["status"] == "executed"
    again = run(system.gate.confirm(p["proposal_id"], p["confirm_digest"], ANA))
    assert again.get("reason") in ("not_awaiting_user", "already_executed")
    assert len(system.store.effects("refund:QM-10001")) == 1
    p2 = system.gate.propose("w2", "issue_refund", {"order_id": "QM-10001", "amount": 59.99, "reason": "damaged"}, ANA)
    assert p2["status"] == "rejected"  # already refunded


def test_agent_or_requester_cannot_self_authorize(system):
    ORDERS["QM-10001"]["total"] = 180.0
    p = system.gate.propose("w3", "issue_refund", {"order_id": "QM-10001", "amount": 180.0, "reason": "damaged"}, ANA)
    assert p["status"] == "pending_human"
    assert run(system.gate.human_approve(p["proposal_id"], dict(ANA, scope="approver")))["reason"] == \
        "approver_not_authorised"
    agent_ident = {"sub": "support-agent", "tenant": "quillmoor", "scope": "chat orders:read"}
    assert run(system.gate.human_approve(p["proposal_id"], agent_ident))["status"] == "rejected"
    assert run(system.gate.human_approve(p["proposal_id"], APPROVER))["status"] == "executed"


def test_premium_limit_from_token_tier(system):
    ORDERS["QM-10001"]["total"] = 180.0
    prem = dict(ANA, tier="premium")
    assert system.gate.propose("w4", "issue_refund", {"order_id": "QM-10001", "amount": 180.0, "reason": "damaged"}, prem)["status"] \
        == "pending_confirmation"
