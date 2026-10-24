"""Hallucination-controls evidence board.

Reads docs/results/hallucination.json (deterministic, drift-checked). Writes docs/img/hallucination_board.png
and docs/results/hallucination_board.json (the numbers drawn). --check re-renders into a temp file and fails
if the board numbers drift from the results.
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
RES = ROOT / "docs/results/hallucination.json"
PNG = ROOT / "docs/img/hallucination_board.png"
JSON = ROOT / "docs/results/hallucination_board.json"


def numbers() -> dict:
    r = json.loads(RES.read_text())
    return {
        "note": r["note"], "question_set": r["question_set"], "seeds": r["seeds"],
        "ladder": [{k: x[k] for k in ("config", "wrong_to_customer", "held_back_rate", "false_abstain_rate",
                                       "cost_multiplier")} for x in r["ladder"]],
        "min_score_sweep": r["min_score_sweep"], "logprob_sweep": r["logprob_sweep"],
        "self_consistency_sweep": r["self_consistency_sweep"],
        "isolated": {k: {m: v[m] for m in ("wrong_to_customer", "false_abstain_rate", "cost_multiplier")}
                     for k, v in r["isolated_controls"].items()},
    }


def _plt():
    import logging

    import matplotlib
    matplotlib.use("Agg")
    matplotlib.rcParams["text.parse_math"] = False
    import matplotlib.pyplot as plt
    logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)
    return plt


def draw(n: dict, path: pathlib.Path) -> None:
    plt = _plt()
    qs = n["question_set"]
    fig, ax = plt.subplots(2, 2, figsize=(15, 10))
    fig.suptitle(f"Hallucination controls - simulated (seeded fake model), exact counts on "
                 f"{sum(qs.values())} labelled questions x {n['seeds']} seeds "
                 f"({qs['answerable']} answerable, {qs['unanswerable']} unanswerable, {qs['stale']} stale)",
                 fontsize=13)
    lad = n["ladder"]
    y = list(range(len(lad)))
    a = ax[0, 0]
    a.barh([i - 0.2 for i in y], [x["wrong_to_customer"] * 100 for x in lad], height=0.4, color="#d62728",
           label="wrong answers reaching customers %")
    a.barh([i + 0.2 for i in y], [x["false_abstain_rate"] * 100 for x in lad], height=0.4, color="#1f77b4",
           label="answerable questions held back %")
    for i, x in enumerate(lad):
        a.text(x["wrong_to_customer"] * 100 + 0.5, i - 0.2, f"{x['wrong_to_customer']:.1%}", va="center",
               fontsize=8)
        a.text(x["false_abstain_rate"] * 100 + 0.5, i + 0.2, f"{x['false_abstain_rate']:.1%}  "
               f"cost x{x['cost_multiplier']}", va="center", fontsize=8)
    a.set_yticks(y, [x["config"] for x in lad], fontsize=8)
    a.invert_yaxis()
    a.set_xlim(0, 80)
    a.legend(fontsize=8, loc="center right")
    a.set_title("A. Stacking the controls (cost = model calls per question)")

    b = ax[0, 1]
    ss = n["min_score_sweep"]
    xs = [s["min_score"] for s in ss]
    b.plot(xs, [s["wrong_to_customer"] * 100 for s in ss], "o-", color="#d62728", label="wrong to customer %")
    b.plot(xs, [s["false_abstain_rate"] * 100 for s in ss], "s-", color="#1f77b4", label="false abstain %")
    b.axvline(0.5, color="grey", linestyle="--")
    b.text(0.51, 55, "chosen 0.5", fontsize=8, color="grey")
    b.set_xlabel("minimum retrieval score (0 = off)")
    b.set_title("B. Retrieval score threshold sweep (control alone)")
    b.legend(fontsize=8)

    c = ax[1, 0]
    ls = [s for s in n["logprob_sweep"] if s["cutoff"] is not None]
    xs = [s["cutoff"] for s in ls]
    c.plot(xs, [s["wrong_to_customer"] * 100 for s in ls], "o-", color="#d62728", label="wrong to customer %")
    c.plot(xs, [s["false_abstain_rate"] * 100 for s in ls], "s-", color="#1f77b4", label="false abstain %")
    off = n["logprob_sweep"][0]
    c.axhline(off["wrong_to_customer"] * 100, color="#d62728", linestyle=":")
    c.text(-3.0, off["wrong_to_customer"] * 100 + 0.5, "gate off", fontsize=8, color="#d62728")
    c.set_xlabel("cutoff on the least confident token's log-prob")
    c.set_title("C. Log-prob cutoff sweep (control alone)")
    c.legend(fontsize=8)

    d = ax[1, 1]
    sc = n["self_consistency_sweep"]
    labels = [f"N={s['n']}, agree>={s['min_agree']}" for s in sc]
    d.bar(range(len(sc)), [s["wrong_to_customer"] * 100 for s in sc], color="#d62728")
    for i, s in enumerate(sc):
        d.text(i, s["wrong_to_customer"] * 100 + 0.2, f"{s['wrong_to_customer']:.2%}\ncost x{s['cost_multiplier']:g}",
               ha="center", fontsize=8)
    d.set_xticks(range(len(sc)), labels, fontsize=8)
    d.set_ylabel("wrong answers reaching customers %")
    d.set_ylim(0, max(s["wrong_to_customer"] for s in sc) * 100 * 1.4 + 1)
    d.set_title("D. Self-consistency: fewer wrong answers, N times the calls")
    fig.tight_layout()
    fig.savefig(path, dpi=100)
    plt.close(fig)


def main(argv) -> int:
    n = numbers()
    js = json.dumps(n, indent=2) + "\n"
    if "--check" in argv:
        tmp = ROOT / "hallucination_board.check.png"
        draw(n, tmp)
        tmp.unlink()
        if not JSON.exists() or JSON.read_text() != js:
            print("drift: docs/results/hallucination_board.json - run python scripts/hallucination_board.py")
            return 1
        print("hallucination board numbers up to date; figure renders")
        return 0
    JSON.write_text(js)
    draw(n, PNG)
    print(f"wrote {PNG.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
