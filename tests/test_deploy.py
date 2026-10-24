"""Deploy artifacts stay consistent with the code: probes, rollout strategy, alert metric names, blocklist."""
import pathlib
import re
import sys

import yaml

from sysdesign.observability import Metrics
from sysdesign.release import error_rate_alert

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_deployment_has_probes_and_zero_unavailable_rollout():
    d = yaml.safe_load((ROOT / "k8s/deployment.yaml").read_text())
    strat = d["spec"]["strategy"]
    assert strat["type"] == "RollingUpdate" and strat["rollingUpdate"]["maxUnavailable"] == 0
    c = d["spec"]["template"]["spec"]["containers"][0]
    assert c["readinessProbe"]["httpGet"]["path"] == "/readyz"
    assert c["livenessProbe"]["httpGet"]["path"] == "/healthz"


def test_alert_rules_only_use_metrics_the_app_exports():
    exported = {m.name for m in Metrics().registry.collect()}
    exported |= {f"{n}_total" for n in exported} | {f"{n}_bucket" for n in exported}
    rules = yaml.safe_load((ROOT / "deploy/prometheus/alerts.yml").read_text())
    exprs = [r["expr"] for g in rules["groups"] for r in g["rules"]]
    assert any("requests_total" in e and "5.." in e for e in exprs), "error-rate alert missing"
    for e in exprs:
        for name in re.findall(r"\b([a-z_]+(?:_total|_bucket|_seconds))\b", e):
            assert name in exported, f"alert uses unknown metric {name}"


def test_prometheus_rule_threshold_matches_code_rule():
    rules = (ROOT / "deploy/prometheus/alerts.yml").read_text()
    assert "> 0.01" in rules
    assert error_rate_alert([(100, 2), (100, 2)])["firing"]
    assert not error_rate_alert([(100, 1), (100, 1)])["firing"]


def test_desired_state_file_and_argocd_marked_planned():
    assert yaml.safe_load((ROOT / "deploy/version.yaml").read_text())["version"].startswith("v")
    assert "PLANNED" in (ROOT / "deploy/argocd/application.yaml").read_text()


def test_brand_blocklist_is_non_empty_and_catches_fixture():
    sys.path.insert(0, str(ROOT / "scripts"))
    import check_brands

    assert len(check_brands.TERM_HASHES) > 50
    assert check_brands.find_hits("Quillmoor Market sells books") == []
    fake = check_brands.digest("zorblax")
    assert check_brands.find_hits("Zorblax", term_hashes={fake}, spelling_hashes={check_brands.digest("Zorblax")})
