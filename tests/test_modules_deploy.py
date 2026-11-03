"""Deploy artifacts stay consistent with the code: probes, rollout strategy, alert metric names."""
import json
import pathlib
import re

import yaml

from sysdesign.ext.release import error_rate_alert, read_version
from sysdesign.observability import Metrics

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _exported():
    names = {m.name for m in Metrics().registry.collect()}
    return names | {f"{n}_total" for n in names} | {f"{n}_bucket" for n in names}


def test_deployment_has_probes_and_zero_unavailable_rollout():
    d = yaml.safe_load((ROOT / "k8s/deployment.yaml").read_text())
    strat = d["spec"]["strategy"]
    assert strat["type"] == "RollingUpdate" and strat["rollingUpdate"]["maxUnavailable"] == 0
    c = d["spec"]["template"]["spec"]["containers"][0]
    assert c["readinessProbe"]["httpGet"]["path"] == "/readyz"
    assert c["livenessProbe"]["httpGet"]["path"] == "/healthz"
    assert c["ports"][0]["containerPort"] == 8000  # matches the Dockerfile CMD


def test_alerts_and_dashboard_only_use_metrics_the_app_exports():
    exported = _exported()
    rules = yaml.safe_load((ROOT / "deploy/alerts.yml").read_text())
    exprs = [r["expr"] for g in rules["groups"] for r in g["rules"]]
    dash = json.loads((ROOT / "deploy/grafana/provisioning/dashboards/kestrel.json").read_text())
    exprs += [t["expr"] for p in dash["panels"] for t in p.get("targets", [])]
    assert any("sd_http_requests_total" in e and "5.." in e for e in exprs), "error-rate alert missing"
    for e in exprs:
        for name in re.findall(r"\b([a-z_]+(?:_total|_bucket|_seconds))\b", e):
            assert name in exported, f"uses unknown metric {name}"


def test_prometheus_error_threshold_matches_code_rule():
    assert "> 0.01" in (ROOT / "deploy/alerts.yml").read_text()
    assert error_rate_alert([(100, 2), (100, 2)])["firing"]
    assert not error_rate_alert([(100, 1), (100, 1)])["firing"]


def test_desired_state_file_and_argocd_marked_planned():
    assert read_version(ROOT / "deploy/version.yaml").startswith("v")
    assert "PLANNED" in (ROOT / "deploy/argocd/application.yaml").read_text()
