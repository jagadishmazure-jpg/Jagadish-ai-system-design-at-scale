"""MLOps loop for the small intent classifier and the prompt/model pins.

- Training and inference are split: train_intent_model() runs offline and writes an artifact;
  serving only loads artifacts by hash from a local registry JSON (DVC/MLflow are planned only).
- The code/data/model triangle is versioned: each registry entry stores git commit, data hash and
  model hash; a model whose data hash does not match the dataset on disk is refused.
- One shared feature function (features()) is used by training AND serving: no skew.
- Drift: PSI for data drift (input features), prediction drift (label mix) and concept drift
  (accuracy drop on fresh labelled samples). Drift triggers retrain -> eval gate -> promote.
- Release router: canary (percent), shadow (run both, serve old) and A/B (sticky by user hash).
- Version pinning: a vendor model update runs the golden set and rolls back on regression.
"""
from __future__ import annotations

import hashlib
import json
import math
import pathlib
import subprocess
from collections import Counter

from .rag import tokens

FEATURE_VOCAB = ["refund", "return", "track", "where", "cancel", "charg", "ship", "policy", "damag", "order"]


def features(text: str) -> list[float]:
    """The single feature definition shared by training and serving."""
    t = tokens(text)
    return [float(sum(1 for w in t if w.startswith(v))) for v in FEATURE_VOCAB] + [min(len(t), 50) / 50]


def sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()[:16]


def train_intent_model(rows: list[tuple[str, str]]) -> dict:
    """Nearest-centroid classifier: deterministic, tiny, good enough to exercise the loop."""
    by: dict[str, list[list[float]]] = {}
    for text, label in rows:
        by.setdefault(label, []).append(features(text))
    cents = {k: [sum(c) / len(v) for c in zip(*v, strict=False)] for k, v in by.items()}
    return {"centroids": cents, "data_hash": sha(rows)}


def predict(model: dict, text: str) -> str:
    f = features(text)
    return min(model["centroids"], key=lambda k: sum((a - b) ** 2 for a, b in zip(f, model["centroids"][k], strict=False)))


def accuracy(model: dict, rows) -> float:
    return sum(predict(model, t) == y for t, y in rows) / max(1, len(rows))


class Registry:
    def __init__(self, path: pathlib.Path) -> None:
        self.path = path
        self.data = json.loads(path.read_text()) if path.exists() else {"models": [], "production": None}

    def _commit(self) -> str:
        try:
            return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                                  check=True).stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            return "uncommitted"

    def register(self, model: dict, metrics: dict) -> str:
        mh = sha(model["centroids"])
        self.data["models"].append({"model_hash": mh, "data_hash": model["data_hash"], "git": self._commit(),
                                    "metrics": metrics, "artifact": model})
        self.path.write_text(json.dumps(self.data, indent=1))
        return mh

    def promote(self, mh: str) -> None:
        self.data["production"] = mh
        self.path.write_text(json.dumps(self.data, indent=1))

    def load(self, mh: str, dataset_rows) -> dict:
        e = next(m for m in self.data["models"] if m["model_hash"] == mh)
        if e["data_hash"] != sha(dataset_rows):
            raise ValueError("data hash mismatch: model was trained on different data")
        if sha(e["artifact"]["centroids"]) != mh:
            raise ValueError("artifact hash mismatch")
        return e["artifact"]


def psi(expected: list[float], actual: list[float], bins: int = 10) -> float:
    lo, hi = min(expected + actual), max(expected + actual)
    if hi == lo:
        return 0.0
    def hist(xs):
        c = Counter(min(bins - 1, int((x - lo) / (hi - lo) * bins)) for x in xs)
        return [(c.get(i, 0) + 0.5) / (len(xs) + 0.5 * bins) for i in range(bins)]
    e, a = hist(expected), hist(actual)
    return sum((ai - ei) * math.log(ai / ei) for ei, ai in zip(e, a, strict=False))


def label_psi(expected: list[str], actual: list[str]) -> float:
    labels = sorted(set(expected) | set(actual))
    ce, ca = Counter(expected), Counter(actual)
    e = [(ce[k] + 0.5) / (len(expected) + 0.5 * len(labels)) for k in labels]
    a = [(ca[k] + 0.5) / (len(actual) + 0.5 * len(labels)) for k in labels]
    return sum((ai - ei) * math.log(ai / ei) for ei, ai in zip(e, a, strict=False))


def drift_report(model, ref_rows, live_texts, live_labelled=None, threshold: float = 0.2,
                 acc_drop: float = 0.1) -> dict:
    ref_len = [features(t)[-1] for t, _ in ref_rows]
    live_len = [features(t)[-1] for t in live_texts]
    data = psi(ref_len, live_len)
    pred = label_psi([predict(model, t) for t, _ in ref_rows], [predict(model, t) for t in live_texts])
    concept = None
    if live_labelled:
        concept = accuracy(model, ref_rows) - accuracy(model, live_labelled)
    flags = {"data_drift": data > threshold, "prediction_drift": pred > threshold,
             "concept_drift": concept is not None and concept > acc_drop}
    return {"data_psi": round(data, 3), "prediction_psi": round(pred, 3),
            "accuracy_drop": None if concept is None else round(concept, 3), **flags,
            "retrain": any(flags.values())}


def retrain_gate(old: dict, new: dict, golden) -> dict:
    a_old, a_new = accuracy(old, golden), accuracy(new, golden)
    return {"old": round(a_old, 3), "new": round(a_new, 3), "promote": a_new >= a_old}


class ReleaseRouter:
    def __init__(self, mode: str, canary_pct: int = 10) -> None:
        self.mode, self.canary_pct = mode, canary_pct
        self.shadow_diffs = 0

    def choose(self, user: str) -> str:
        b = int(hashlib.sha256(user.encode()).hexdigest(), 16) % 100
        if self.mode == "canary":
            return "candidate" if b < self.canary_pct else "stable"
        if self.mode == "ab":
            return "candidate" if b % 2 else "stable"
        return "stable"

    def serve(self, user: str, stable, candidate):
        if self.mode == "shadow":
            s, c = stable(), candidate()
            if s != c:
                self.shadow_diffs += 1
            return s                       # shadow output is logged, never served
        return candidate() if self.choose(user) == "candidate" else stable()


class VersionPin:
    """Pin model+prompt versions; a vendor update is adopted only if the golden set does not regress."""

    def __init__(self, model_version: str, prompt_version: str) -> None:
        self.current = {"model": model_version, "prompt": prompt_version}
        self.history = [dict(self.current)]

    def try_update(self, new_model: str, score_fn, golden, tolerance: float = 0.0) -> dict:
        base = score_fn(self.current["model"], golden)
        cand = score_fn(new_model, golden)
        if cand + tolerance < base:
            return {"adopted": False, "rolled_back_to": self.current["model"], "baseline": base, "candidate": cand}
        self.current["model"] = new_model
        self.history.append(dict(self.current))
        return {"adopted": True, "baseline": base, "candidate": cand}
