import pathlib
import subprocess

import pytest

from sysdesign import evals, mlops
from sysdesign.build import build
from sysdesign.release import error_rate_alert, on_alert_revert, read_version
from tests.conftest import ANA, run


def test_eval_suite_passes_and_gate():
    r = evals.run(build())
    assert r["task_success_rate"] == 1.0 and r["scenarios"] == len(evals.SUITE)
    cats = {s.category for s in evals.SUITE}
    assert {"happy_path", "ambiguous", "malicious", "tool_failure", "policy_edge", "escalation"} <= cats


def test_polished_answer_with_wrong_retrieval_fails():
    sc = evals.Scenario("x", "retrieval", "Do you ship internationally?", expect_chunks=["returns-window"],
                        expect_answer_has=["International"])
    s = build()
    out = run(s.agent.handle(sc.message, ANA))
    score = evals.score(sc, out)
    assert score["final_answer"] and not score["retrieval"] and not score["success"]
    assert evals.attribute(score, out) == "retrieval"


def test_repeated_tool_call_flagged():
    s = build()
    out = run(s.agent.handle("Where is my order QM-10002?", ANA))
    step = [x for x in out["trace"].steps if x.name == "tool_call"][0]
    out["trace"].steps.extend([step, step])
    assert not evals.score(evals.SUITE[0], out)["no_repeat_calls"]


def test_lying_tool_detected_by_side_effect_check(system):
    assert not evals.verify_side_effect(system.store, "refund:QM-10001")  # tool may say ok; record says no


def test_feedback_loop_redacts_pii(tmp_path):
    s = build()
    msg = "my email is ana@example.com and card 4111 1111 1111 1111, refund please"
    out = run(s.agent.handle(msg, ANA))
    fb = evals.FeedbackLoop(tmp_path / "regress.json")
    sc = fb.from_trace(msg, out, "thumbs_down", expect_intent="refund_request")
    text = (tmp_path / "regress.json").read_text()
    assert "ana@example.com" not in text and "4111" not in text and sc.category == "regression:thumbs_down"


def test_deploy_gate_blocks_regression():
    bad = run(evals.gate_change(build, {}, {"router": False}))   # "simplify: send everything to one model"
    assert bad["baseline"] == 1.0 and bad["candidate"] < 1.0 and bad["deploy"] is False
    ok = run(evals.gate_change(build, {}, {"cache": False}))
    assert ok["deploy"] is True


def test_zscore_and_drift_watcher():
    vals = [1.0] * 30 + [50.0]
    assert evals.zscore_outliers(vals) == [30]
    dw = evals.DriftWatcher(["faq_policy"] * 50 + ["order_status"] * 50, every=1)
    for _ in range(40):
        dw.observe("billing_dispute")
    assert dw.check()["drift"]


def test_feature_skew_registry_and_psi(tmp_path):
    rows = [("where is my order", "order_status"), ("refund my damaged item", "refund_request"),
            ("what is your return policy", "faq_policy"), ("track my order", "order_status")] * 5
    m = mlops.train_intent_model(rows)
    assert mlops.features("track order") == mlops.features("track order")  # same function both sides
    reg = mlops.Registry(tmp_path / "registry.json")
    h = reg.register(m, {"acc": mlops.accuracy(m, rows)})
    reg.promote(h)
    assert reg.load(h, rows)["data_hash"] == m["data_hash"]
    with pytest.raises(ValueError):
        reg.load(h, rows[:3])  # data hash mismatch refused
    rep = mlops.drift_report(m, rows, ["cancel cancel cancel my subscription please now"] * 30)
    assert rep["retrain"]


def test_version_pin_rolls_back():
    vp = mlops.VersionPin("m-1", "p-1")
    r = vp.try_update("m-2", lambda m, g: 0.9 if m == "m-1" else 0.7, golden=[])
    assert not r["adopted"] and vp.current["model"] == "m-1"


def test_canary_shadow_ab():
    c = mlops.ReleaseRouter("canary", 10)
    share = sum(c.choose(f"u{i}") == "candidate" for i in range(2000)) / 2000
    assert 0.05 < share < 0.15
    sh = mlops.ReleaseRouter("shadow")
    assert sh.serve("u", lambda: "old", lambda: "new") == "old" and sh.shadow_diffs == 1


def test_error_rate_alert_rule():
    assert not error_rate_alert([(1000, 5), (1000, 9)])["firing"]       # 0.5%, 0.9%
    assert not error_rate_alert([(1000, 50)])["firing"]                 # one window only
    assert error_rate_alert([(1000, 20), (1000, 30)])["firing"]
    assert not error_rate_alert([(10, 5), (10, 5)])["firing"]           # too little traffic to page
    with pytest.raises(ValueError):
        error_rate_alert([(10, -1)])


def test_alert_after_release_triggers_git_revert(tmp_path):
    repo = tmp_path / "gitops"
    (repo / "deploy").mkdir(parents=True)
    vf = repo / "deploy" / "version.yaml"
    g = ["git", "-C", str(repo), "-c", "user.email=t@example.invalid", "-c", "user.name=t"]
    subprocess.check_call(["git", "init", "-q", str(repo)])
    vf.write_text('app: support-agent\nimage_tag: "v2"\n')
    subprocess.check_call(g + ["add", "."])
    subprocess.check_call(g + ["commit", "-qm", "release v2"])
    vf.write_text('app: support-agent\nimage_tag: "v3"\n')
    subprocess.check_call(g + ["commit", "-qam", "release v3"])
    assert read_version(vf) == "v3"
    alert = error_rate_alert([(500, 40), (500, 45)])   # v3 is failing ~8-9% of requests
    import os
    os.environ.setdefault("GIT_AUTHOR_NAME", "t")
    os.environ.setdefault("GIT_AUTHOR_EMAIL", "t@example.invalid")
    os.environ.setdefault("GIT_COMMITTER_NAME", "t")
    os.environ.setdefault("GIT_COMMITTER_EMAIL", "t@example.invalid")
    out = on_alert_revert(pathlib.Path(repo), alert)
    assert out["reverted"] and out["now"] == "v2"
