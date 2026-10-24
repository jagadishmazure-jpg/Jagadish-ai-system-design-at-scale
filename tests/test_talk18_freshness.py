"""Talk 18 (A): stale policy answers while every ops metric stays green."""
import json
import pathlib
from datetime import date

import pytest

from sysdesign.ext.freshness import (
    POLICIES,
    TODAY,
    PolicyStore,
    PolicyVersion,
    ReviewQueue,
    claimed_values,
    fake_model,
    freshness_check,
    replay,
    results,
    verify,
)
from sysdesign.ext.grounding import check as grounded

ROOT = pathlib.Path(__file__).resolve().parent.parent
store = PolicyStore()
Q = "Can I return my sofa for a refund? It arrived 45 days ago."
NEW = "Per our policy: items can be returned for a refund within 30 days of delivery."
OLD = "Per our policy: items can be returned for a refund within 60 days of delivery."


def test_every_doc_has_exactly_one_version_in_force_today():
    for doc in {v.doc_id for v in POLICIES}:
        assert sum(v.in_force(TODAY) for v in POLICIES if v.doc_id == doc) == 1


def test_versions_do_not_overlap_or_leave_gaps():
    for doc in {v.doc_id for v in POLICIES}:
        vs = sorted((v for v in POLICIES if v.doc_id == doc), key=lambda v: v.version)
        for a, b in zip(vs, vs[1:]):
            assert a.superseded_on == b.effective_from


def test_in_force_boundaries():
    v1 = POLICIES[0]
    assert v1.in_force(date(2026, 6, 30)) and not v1.in_force(date(2026, 7, 1))
    assert not v1.in_force(date(2024, 12, 31))


def test_incident_index_returns_superseded_60_day_policy():
    top = store.search(Q, TODAY, exclude_superseded=False)[0]
    assert top.rules == {"refund_window_days": 60} and not top.in_force(TODAY)


def test_dated_retrieval_excludes_superseded():
    top = store.search(Q, TODAY)[0]
    assert top.rules == {"refund_window_days": 30} and top.in_force(TODAY)


def test_dated_retrieval_as_of_past_date_returns_old_policy():
    assert store.search(Q, date(2026, 3, 1))[0].rules["refund_window_days"] == 60


def test_search_returns_nothing_for_unrelated_query():
    assert store.search("zebra quantum", TODAY) == []


def test_current_value_and_superseded_values():
    assert store.current_value("refund_window_days", TODAY) == 30
    assert store.superseded_values("refund_window_days", TODAY) == {60}
    assert store.current_value("nope", TODAY) is None


def test_future_version_is_not_counted_as_superseded():
    s = PolicyStore(POLICIES + [PolicyVersion("refund-policy", 3, date(2027, 1, 1), None, "x", {"refund_window_days": 14})])
    assert s.superseded_values("refund_window_days", TODAY) == {60}


@pytest.mark.parametrize("text,val", [("60-day refund window", 60), ("refunds within 30 days", 30),
                                      ("You can return it within 60 days", 60)])
def test_claimed_refund_values(text, val):
    assert val in claimed_values(text)["refund_window_days"]


def test_freshness_flags_stale_60_day_answer():
    r = freshness_check(OLD, store, TODAY)
    assert not r["fresh"] and r["stale"] == [{"rule": "refund_window_days", "claimed": 60, "current": 30}]


def test_freshness_passes_current_answer():
    assert freshness_check(NEW, store, TODAY)["fresh"]


def test_freshness_ignores_values_never_in_any_policy():
    assert freshness_check("refunds within 45 days", store, TODAY)["fresh"]  # wrong, but not stale: verifier's job


def test_freshness_flags_old_warranty():
    assert not freshness_check("furniture is covered for 1 year", store, TODAY)["fresh"]


def test_grounding_reused_blocks_invented_number():
    # same check as test_grounding_blocks_invented_number (talk 3), applied to a policy answer
    assert not grounded(NEW + " You also get a 15% restocking credit.", [POLICIES[1].text])["grounded"]


def test_verify_sends_grounded_fresh_answer():
    v = verify(NEW, [POLICIES[1].text], store, TODAY)
    assert v.route == "send" and v.confidence == 1.0 and v.groundedness == 1.0


def test_verify_routes_stale_answer_to_review_even_if_grounded_in_old_doc():
    v = verify(OLD, [POLICIES[0].text], store, TODAY)
    assert v.groundedness == 1.0 and not v.fresh and v.route == "review"


def test_verify_routes_invented_number_to_review():
    v = verify(NEW + " You also get a 15% restocking credit.", [POLICIES[1].text], store, TODAY)
    assert v.groundedness < 1 and v.route == "review" and any(r.startswith("ungrounded") for r in v.reasons)


def test_verify_routes_pii_to_review():
    v = verify(NEW + " We will email you at sam.rowe@example.com.", [POLICIES[1].text], store, TODAY)
    assert not v.safe and v.route == "review" and "pii" in v.reasons


def test_verify_threshold_is_configurable():
    v = verify(NEW + " Also 15% off.", [POLICIES[1].text], store, TODAY, threshold=0.0)
    assert v.route == "send"


def test_review_queue_stores_redacted_and_approves():
    q = ReviewQueue()
    v = verify(NEW + " Email sam.rowe@example.com", [POLICIES[1].text], store, TODAY)
    i = q.put("my email is sam.rowe@example.com", NEW + " Email sam.rowe@example.com", v)
    assert "sam.rowe@example.com" not in json.dumps(q.items)
    assert q.approve(i, NEW)["status"] == "approved"


def test_fake_model_without_docs_defers():
    import random
    assert "not sure" in fake_model("x", [], random.Random(1), 0, 0)


def test_replay_ops_dashboard_green_quality_red_before():
    r = replay()
    assert r["ops_dashboard"] == "green" and r["ops"]["error_rate"] == 0 and r["ops"]["uptime"] == 1.0
    assert r["quality_dashboard"] == "red" and r["wrong_sent"]["freshness"] > 0


def test_replay_ops_identical_before_and_after():
    assert replay()["ops"] == replay(filter_superseded=True, verifier=True)["ops"]


def test_replay_after_fix_sends_zero_wrong():
    r = replay(filter_superseded=True, verifier=True)
    assert r["quality_dashboard"] == "green" and sum(r["wrong_sent"].values()) == 0
    assert r["wrong_generated"]["freshness"] == 0


def test_verifier_alone_catches_stale_but_reviews_far_more():
    vo, after = replay(verifier=True), replay(filter_superseded=True, verifier=True)
    assert sum(vo["wrong_sent"].values()) == 0
    assert vo["sent_to_review"] > 3 * after["sent_to_review"]


def test_replay_is_deterministic():
    assert replay(seed=5) == replay(seed=5)


def test_committed_results_match_fresh_run():
    committed = json.loads((ROOT / "docs/results/talk18.json").read_text())
    assert committed["freshness"] == json.loads(json.dumps(results()))
