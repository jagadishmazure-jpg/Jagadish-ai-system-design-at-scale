"""Release safety: the error-rate alert that fires after a bad release, and the GitOps decision it
drives (revert the commit that changed deploy/version.yaml). Mirrors the Prometheus rule in
deploy/prometheus/alerts.yml so the same threshold is unit-tested here."""
from __future__ import annotations

import pathlib
import re
import subprocess

ERROR_RATE_THRESHOLD = 0.01   # > 1% of requests failing
FOR_WINDOWS = 2               # must hold for 2 consecutive evaluation windows (like `for: 2m`)
MIN_REQUESTS = 50             # do not page on 1 failure out of 3 requests


def error_rate_alert(windows: list[tuple[int, int]]) -> dict:
    """windows: [(requests, 5xx errors)] oldest -> newest, one per evaluation interval."""
    firing_streak = 0
    for req, err in windows:
        if req < 0 or err < 0 or err > req:
            raise ValueError("invalid counts")
        firing_streak = firing_streak + 1 if req >= MIN_REQUESTS and err / req > ERROR_RATE_THRESHOLD else 0
    return {"firing": firing_streak >= FOR_WINDOWS, "streak": firing_streak}


def read_version(path: pathlib.Path) -> str:
    m = re.search(r"^\s*image_tag:\s*\"?([\w.\-]+)\"?\s*$", path.read_text(), re.M)
    if not m:
        raise ValueError("no image_tag in desired state")
    return m.group(1)


def last_release_commit(repo: pathlib.Path, file: str = "deploy/version.yaml") -> str:
    return subprocess.check_output(["git", "-C", str(repo), "log", "-n1", "--format=%H", "--", file],
                                   text=True).strip()


def on_alert_revert(repo: pathlib.Path, alert: dict, file: str = "deploy/version.yaml") -> dict:
    """If the alert fires, revert the last commit that touched the desired-state file.
    The GitOps controller (Argo CD, planned) or scripts/gitops_rollback.sh then syncs the cluster."""
    if not alert["firing"]:
        return {"reverted": False}
    sha = last_release_commit(repo, file)
    subprocess.check_call(["git", "-C", str(repo), "revert", "--no-edit", sha],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"reverted": True, "reverted_commit": sha, "now": read_version(repo / file)}
