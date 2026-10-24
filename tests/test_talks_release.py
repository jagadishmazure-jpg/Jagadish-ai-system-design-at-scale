"""Talks 6 and 13: MLOps hygiene and release safety (alert -> git revert)."""
import os
import pathlib
import subprocess

import pytest

from sysdesign.ext import mlops
from sysdesign.ext.release import error_rate_alert, on_alert_revert, read_version


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

    os.environ.setdefault("GIT_AUTHOR_NAME", "t")
    os.environ.setdefault("GIT_AUTHOR_EMAIL", "t@example.invalid")
    os.environ.setdefault("GIT_COMMITTER_NAME", "t")
    os.environ.setdefault("GIT_COMMITTER_EMAIL", "t@example.invalid")
    out = on_alert_revert(pathlib.Path(repo), alert)
    assert out["reverted"] and out["now"] == "v2"
