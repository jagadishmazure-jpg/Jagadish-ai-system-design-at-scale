"""Hallucination controls: log-prob gate, self-consistency, retrieval threshold, citation enforcement,
few-shot 'I don't know' prompt with strict JSON output. Simulated (seeded fake model)."""
import json
import pathlib
import subprocess
import sys

from sysdesign.ext.freshness import TODAY, PolicyStore, ReviewQueue
from sysdesign.ext.hallucination import (
    IDK_PROMPT,
    LADDER,
    QUESTIONS,
    Config,
    FakeLM,
    Generation,
    enforce_citations,
    evaluate,
    logprob_gate,
    retrieve,
    run_one,
    safe_log_line,
    self_consistency,
    validate_output,
)

ROOT = pathlib.Path(__file__).resolve().parent.parent
store = PolicyStore()
RES = json.loads((ROOT / "docs/results/hallucination.json").read_text())


def _out(answer, cites=(), abstain=False):
    return {"answer": answer, "citations": list(cites), "abstain": abstain}


def test_question_set_has_all_three_kinds():
    kinds = {q["kind"] for q in QUESTIONS}
    assert kinds == {"answerable", "unanswerable", "stale"}
    assert len(QUESTIONS) == 30


def test_retrieval_scores_are_shares_and_dated_index_hides_old_versions():
    chunks = retrieve("Is the furniture warranty 1 year?", store, TODAY)
    assert all(0 <= c["score"] <= 1 for c in chunks)
    assert chunks[0]["id"] == "warranty-policy@v2"
    undated = retrieve("Is the furniture warranty 1 year?", store, TODAY, dated=False)
    assert undated[0]["id"] == "warranty-policy@v1"  # the incident tie-break, reproduced


def test_min_score_drops_weak_chunks_and_pipeline_abstains_without_calling_model():
    assert retrieve("Can I get a refund on a gift card?", store, TODAY, min_score=0.5) == []
    before = FakeLM.calls
    o = run_one("Can I get a refund on a gift card?", Config("t", dated=True, min_score=0.5), FakeLM(1), store)
    assert o.action == "abstain" and o.reasons == ["below_min_score"] and FakeLM.calls == before


def test_logprob_gate_uses_least_confident_token():
    g = Generation("x", ["a", "b", "c"], [-0.01, -0.02, -1.6])
    assert not logprob_gate(g, -1.0)
    assert logprob_gate(g, -2.0)
    assert not logprob_gate(Generation("", [], []), -1.0)


def test_fake_model_exposes_one_logprob_per_token_and_is_deterministic():
    chunks = retrieve("How long is the warranty on furniture defects?", store, TODAY)
    a = FakeLM(3).generate("q", chunks, True, True)
    b = FakeLM(3).generate("q", chunks, True, True)
    assert a == b and len(a.tokens) == len(a.logprobs) and all(lp < 0 for lp in a.logprobs)


def test_self_consistency_majority_and_abstain_on_disagreement():
    good = _out("Per our policy: furniture is covered for 2 years against defects.")
    g = Generation("", [], [])
    idx, share = self_consistency([(g, good)] * 4 + [(g, _out("Yes, within 14 days."))], 3)
    assert idx == 0 and share == 0.8
    mixed = [(g, _out(f"Yes, within {d} days.")) for d in (14, 21, 45, 90, 14)]
    assert self_consistency(mixed, 3) == (None, 0.4)


def test_citation_enforcement_rejects_missing_unknown_superseded_and_unsupported():
    chunks = retrieve("What warranty comes with furniture?", store, TODAY)
    ok = _out("Furniture is covered for 2 years against defects.", ["warranty-policy@v2"])
    assert enforce_citations(ok, chunks, store, TODAY) == []
    assert enforce_citations(_out(ok["answer"]), chunks, store, TODAY) == ["no_citation"]
    assert enforce_citations(_out(ok["answer"], ["warranty-policy@v9"]), chunks, store, TODAY)[0].startswith(
        "unknown_id")
    assert "superseded:warranty-policy@v1" in enforce_citations(
        _out("Covered for 1 year.", ["warranty-policy@v1"]), chunks, store, TODAY)
    assert "unsupported:5" in enforce_citations(
        _out("Covered for 5 years.", ["warranty-policy@v2"]), chunks, store, TODAY)
    assert "not_retrieved:shipping-policy@v1" in enforce_citations(
        _out("Standard takes 5 business days.", ["shipping-policy@v1"]), chunks, store, TODAY)
    assert enforce_citations(_out("I don't know.", abstain=True), chunks, store, TODAY) == []


def test_strict_json_output_rejects_extra_keys_wrong_types_and_prose():
    assert validate_output('{"answer": "x", "citations": ["a"], "abstain": false}')
    assert validate_output('{"answer": "x", "citations": [], "abstain": false, "note": 1}') is None
    assert validate_output('{"answer": "x", "citations": "a", "abstain": false}') is None
    assert validate_output('{"answer": "x", "citations": [1], "abstain": false}') is None
    assert validate_output("Sure! The window is 30 days.") is None
    assert validate_output("[]") is None


def test_idk_prompt_has_refusal_and_answer_examples():
    assert '"abstain": true' in IDK_PROMPT and '"abstain": false' in IDK_PROMPT
    assert "don't know" in IDK_PROMPT


def test_idk_prompt_makes_model_abstain_more_on_unanswerable():
    plain = evaluate(Config("p", dated=True), seeds=5)["by_kind"]["unanswerable"].get("abstain", 0)
    idk = evaluate(Config("i", dated=True, idk_schema=True), seeds=5)["by_kind"]["unanswerable"].get("abstain", 0)
    assert idk > plain


def test_ladder_cuts_wrong_answers_and_shows_the_price():
    lad = RES["ladder"]
    assert lad[0]["wrong_to_customer"] > 0.5
    wrong = [x["wrong_to_customer"] for x in lad]
    assert wrong[1] < wrong[0] and wrong[5] < wrong[2] and wrong[-1] == 0.0
    assert lad[5]["false_abstain_rate"] > lad[1]["false_abstain_rate"]  # safety costs good answers held back
    assert lad[6]["cost_multiplier"] > 2 * lad[7]["cost_multiplier"]  # selective sampling is much cheaper


def test_stale_questions_fixed_by_talk18_and_leftovers_are_invented_extras():
    lad = RES["ladder"]
    assert lad[0]["by_kind"]["stale"]["sent_wrong"] == lad[0]["by_kind"]["stale"]["n"]  # incident: all stale
    assert all(x["by_kind"]["stale"].get("sent_wrong", 0) <= 5 for x in lad[1:])
    assert lad[-1]["by_kind"]["stale"].get("sent_wrong", 0) == 0


def test_threshold_sweeps_trade_wrong_answers_for_false_abstains():
    ss = RES["min_score_sweep"]
    assert ss[-1]["false_abstain_rate"] > ss[0]["false_abstain_rate"]
    assert ss[-1]["wrong_to_customer"] < ss[0]["wrong_to_customer"]
    ls = [s for s in RES["logprob_sweep"] if s["cutoff"] is not None]
    assert ls[0]["false_abstain_rate"] > ls[-1]["false_abstain_rate"]  # strictest cutoff holds most back
    sc = {(s["n"], s["min_agree"]): s for s in RES["self_consistency_sweep"]}
    assert sc[(5, 3)]["cost_multiplier"] == 5.0 and sc[(5, 3)]["wrong_to_customer"] < sc[(1, 1)][
        "wrong_to_customer"]


def test_review_queue_and_logs_are_redacted():
    q = ReviewQueue()
    cfg = LADDER[1]
    for seed in range(20):
        for item in QUESTIONS:
            run_one(item["q"] + " Reach me at sam.rowe@example.com", cfg, FakeLM(seed), store, queue=q)
    assert q.items, "some answers should be held for review"
    assert all("sam.rowe@example.com" not in json.dumps(i) for i in q.items)
    o = run_one("Email sam.rowe@example.com: refund window?", cfg, FakeLM(0), store)
    assert "sam.rowe@example.com" not in safe_log_line("Email sam.rowe@example.com: refund window?", o)


def test_committed_results_and_board_match_a_fresh_run():
    for script in ("scripts/hallucination_bench.py", "scripts/hallucination_board.py"):
        r = subprocess.run([sys.executable, script, "--check"], cwd=ROOT, capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr
