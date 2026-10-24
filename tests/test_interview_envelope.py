"""Interview playbook calculator (sysdesign/ext/envelope.py) and doc integrity."""
import json
import math
import pathlib
import re
import subprocess
import sys

import pytest

from sysdesign.ext import envelope as E

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_talk_10m_traffic():
    r = E.compute(E.TALK_10M)
    assert r["requests_per_day"] == 50_000_000
    assert r["avg_qps"] == pytest.approx(578.7037, rel=1e-6)
    assert r["peak_qps"] == pytest.approx(1736.111, rel=1e-6)


def test_talk_10m_tokens_split():
    r = E.compute(E.TALK_10M)
    assert r["tokens_per_request"] == 3_000
    assert r["tokens_per_day"] == 150e9
    assert r["input_tokens_per_day"] == 125e9 and r["output_tokens_per_day"] == 25e9


def test_cost_by_hand():
    # blended in = .75*.15+.2*1+.05*5 = .5625; out = .75*.6+.2*4+.05*20 = 2.25
    r = E.compute(E.TALK_10M)
    assert r["cost_per_request"] == pytest.approx(2500 * 0.5625e-6 + 500 * 2.25e-6)
    assert r["cost_per_day"] == pytest.approx(126_562.5)


def test_all_frontier_is_much_dearer():
    r = E.compute(E.TALK_10M)
    assert r["all_frontier_cost_per_day"] > 8 * r["cost_per_day"]


def test_talk_200k_traffic_and_tokens():
    r = E.compute(E.TALK_200K)
    assert r["requests_per_day"] == 1_600_000
    assert r["avg_qps"] == pytest.approx(18.5185, rel=1e-4)
    assert r["peak_qps"] == pytest.approx(55.5556, rel=1e-4)
    assert r["tokens_per_day"] == 4.8e9


def test_planted_mistake_daily_passes_conversation_fails():
    p = E.planted_mistake(E.TALK_200K, E.PLANTED_CEILING)
    assert p["daily_check_passes"] is True
    assert p["conversation_check_passes"] is False
    assert p["cost_per_conversation"] == pytest.approx(p["cost_per_day"] / 200_000)
    assert p["cost_per_conversation"] > p["target_per_conversation"]
    assert p["daily_ceiling_implied_per_conversation"] == pytest.approx(0.03)


def test_planted_mistake_needs_target():
    with pytest.raises(ValueError):
        E.planted_mistake(E.TALK_10M, 1.0)


def test_best_mix_meets_target_and_beats_cheapest_on_quality():
    b = E.best_mix(E.TALK_200K, 0.02, min_frontier=0.05)
    assert b["best_cost_per_conversation"] <= 0.02
    assert b["best_mix"]["frontier"] >= 0.05
    assert b["cheapest_cost_per_conversation"] <= b["best_cost_per_conversation"]
    q = lambda m: sum(E.QUALITY[t] * v for t, v in m.items())  # noqa: E731
    assert q(b["best_mix"]) >= q(b["cheapest_mix"])


def test_best_mix_is_optimal_on_grid():
    s, t = E.TALK_200K, 0.02
    b = E.best_mix(s, t, min_frontier=0.05)
    bq = sum(E.QUALITY[k] * v for k, v in b["best_mix"].items())
    for m in E.mixes():
        if m["frontier"] >= 0.05 and E.cost_per_request(s, m) * s.turns <= t:
            assert sum(E.QUALITY[k] * v for k, v in m.items()) <= bq + 1e-9


def test_best_mix_none_when_impossible():
    assert E.best_mix(E.TALK_200K, 0.000001) is None


def test_best_mix_respects_min_mid():
    b = E.best_mix(E.TALK_200K, 0.05, min_mid=0.3)
    assert b["cheapest_mix"]["mid"] >= 0.3


def test_mixes_grid_sums_to_one():
    ms = list(E.mixes(0.25))
    assert len(ms) == 15
    assert all(math.isclose(sum(m.values()), 1.0) for m in ms)


@pytest.mark.parametrize("field", ["users", "sessions_per_user", "turns", "system_tokens", "context_tokens",
                                   "output_tokens"])
def test_rejects_negative_and_nan_inputs(field):
    base = dict(users=1, sessions_per_user=1, turns=1, system_tokens=1, context_tokens=1, output_tokens=1)
    with pytest.raises(ValueError):
        E.Scenario(**{**base, field: -1})
    with pytest.raises(ValueError):
        E.Scenario(**{**base, field: float("nan")})


def test_rejects_negative_price_target_and_peak():
    with pytest.raises(ValueError):
        E.Scenario(1, 1, 1, 1, 1, 1, prices={"small": (-0.1, 1), "mid": (1, 1), "frontier": (1, 1)})
    with pytest.raises(ValueError):
        E.Scenario(1, 1, 1, 1, 1, 1, target_per_conversation=-0.01)
    with pytest.raises(ValueError):
        E.Scenario(1, 1, 1, 1, 1, 1, peak_factor=0.5)
    with pytest.raises(ValueError):
        E.best_mix(E.TALK_200K, -1)


def test_mix_must_sum_to_one():
    with pytest.raises(ValueError):
        E.Scenario(1, 1, 1, 1, 1, 1, mix={"small": 0.5, "mid": 0.2, "frontier": 0.2})
    with pytest.raises(ValueError):
        E.Scenario(1, 1, 1, 1, 1, 1, mix={"giant": 1.0})
    with pytest.raises(ValueError):
        E.Scenario(1, 1, 1, 1, 1, 1, mix={"small": 1.2, "mid": -0.2})


def test_agent_steps_bank_case():
    assert E.case_report("bank")["agent_max_steps"] == 6
    with pytest.raises(ValueError):
        E.agent_steps(0.04, 0, 0)


def test_scale_10x_linear_and_cache_reduces_model_load():
    x = E.scale(E.TALK_200K, 10, cache_hit=0.3)
    base = E.compute(E.TALK_200K)
    assert x["peak_qps"] == pytest.approx(base["peak_qps"] * 10)
    assert x["model_peak_qps"] == pytest.approx(x["peak_qps"] * 0.7)
    assert x["cost_per_day"] == pytest.approx(base["cost_per_day"] * 7)
    with pytest.raises(ValueError):
        E.scale(E.TALK_200K, 10, cache_hit=1.0)


@pytest.mark.parametrize("name", sorted(E.CASES))
def test_every_case_has_a_feasible_mix(name):
    r = E.case_report(name)
    assert r["best"] is not None
    assert r["best"]["best_cost_per_conversation"] <= E.CASES[name]["scenario"].target_per_conversation + 1e-12


def test_low_latency_starting_mix_misses_target():
    assert E.case_report("lowlatency")["within_target"] is False


def test_cli_outputs_json_and_rejects_negative():
    ok = subprocess.run([sys.executable, "-m", "sysdesign.ext.envelope", "--users", "200000", "--turns", "8",
                         "--target", "0.02", "--min-frontier", "0.05"], capture_output=True, text=True, cwd=ROOT)
    assert ok.returncode == 0
    out = json.loads(ok.stdout)
    assert out["requests_per_day"] == 1_600_000 and out["within_target"] is False and out["best"]
    bad = subprocess.run([sys.executable, "-m", "sysdesign.ext.envelope", "--users", "-5"], capture_output=True,
                         text=True, cwd=ROOT)
    assert bad.returncode == 2 and "must be >= 0" in bad.stderr


def test_interview_docs_match_calculator():
    r = subprocess.run([sys.executable, "scripts/render_interview.py", "--check"], capture_output=True, text=True,
                       cwd=ROOT)
    assert r.returncode == 0, r.stdout


def test_case_count_and_sections():
    cases = sorted((ROOT / "docs/interview-cases").glob("0*.md"))
    assert 6 <= len(cases) <= 8
    for c in cases:
        t = c.read_text()
        for sec in ("## Clarify", "## Envelope", "## High-level design", "```mermaid", "## Deep dive",
                    "## Trade-offs", "## Failure modes", "## 10x follow-up", "## Wrap-up"):
            assert sec in t, f"{c.name} missing {sec}"


def test_question_bank_has_40_plus():
    t = (ROOT / "docs/interview-question-bank.md").read_text()
    assert len(re.findall(r"^\d+\. \*\*", t, flags=re.M)) >= 40


def test_every_cited_test_exists():
    names = set()
    for p in (ROOT / "tests").glob("test_*.py"):
        names |= set(re.findall(r"def (test_\w+)", p.read_text()))
    docs = [ROOT / "docs/interview-playbook.md", ROOT / "docs/interview-question-bank.md",
            *(ROOT / "docs/interview-cases").glob("*.md")]
    cited = set()
    for d in docs:
        cited |= set(re.findall(r"`(?:[\w/]+\.py::)?(test_\w+)`", d.read_text()))
    assert cited and not cited - names, cited - names


def test_cited_repo_files_exist():
    docs = [ROOT / "docs/interview-playbook.md", ROOT / "docs/interview-question-bank.md",
            *(ROOT / "docs/interview-cases").glob("*.md")]
    for d in docs:
        for f in re.findall(r"`((?:sysdesign|scripts|tests)/[\w/]+\.py)", d.read_text()):
            assert (ROOT / f).exists(), f"{d.name} cites missing {f}"
