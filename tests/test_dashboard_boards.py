"""Drift checks for the caching "after" board and the talk 18 board / network waterfall."""
from __future__ import annotations

import copy
import importlib.util
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(script: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, f"scripts/{script}", "--check"], cwd=ROOT, capture_output=True, text=True)


def test_caching_after_board_check_passes():
    out = _run("caching_after_board.py")
    assert out.returncode == 0, out.stdout + out.stderr


def test_talk18_board_check_passes():
    out = _run("talk18_evidence_board.py")
    assert out.returncode == 0, out.stdout + out.stderr


def test_caching_after_numbers_are_copied_from_results():
    src = json.loads((ROOT / "docs/results/caching.json").read_text())
    n = _load("caching_after_board").numbers(src)
    after = src["incident"]["august_stable_first"]
    assert n["after_stable_first"]["monthly_bill_usd"] == after["monthly_bill_usd"]
    assert n["after_stable_first"]["token_hit_rate"] == after["token_hit_rate"]
    assert n["change"]["input_cost_reduction"] == src["incident"]["fix"]["input_cost_reduction"]
    assert n["before_timestamp_first"]["ttft_p50_s"] > n["after_stable_first"]["ttft_p50_s"]


def test_caching_after_numbers_follow_a_changed_results_file():
    mod = _load("caching_after_board")
    src = json.loads((ROOT / "docs/results/caching.json").read_text())
    changed = copy.deepcopy(src)
    changed["incident"]["august_stable_first"]["monthly_bill_usd"] += 1
    assert mod.numbers(changed) != mod.numbers(src)


def test_talk18_numbers_follow_a_changed_results_file():
    mod = _load("talk18_evidence_board")
    sim = json.loads((ROOT / "docs/results/talk18.json").read_text())
    meas = json.loads((ROOT / "docs/results/talk18_measured.json").read_text())
    base = mod.numbers(sim, meas)
    changed = copy.deepcopy(sim)
    first = next(iter(changed["waterfall"]["rows"]))
    changed["waterfall"]["rows"][first]["total_ms"] += 1
    assert mod.numbers(changed, meas) != base
    committed = json.loads((ROOT / "docs/results/talk18_evidence_board.json").read_text())
    assert committed == base
