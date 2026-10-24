"""Hallucination-control numbers.

python scripts/hallucination_bench.py          -> docs/results/hallucination.json + tables in
                                                  docs/hallucination-controls.md and README.md
python scripts/hallucination_bench.py --check  -> fails if the committed file differs from a fresh run
Deterministic (seeded fake model), so CI can re-run it exactly. Simulated inputs, exact counts.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from sysdesign.ext.hallucination import results  # noqa: E402

OUT = ROOT / "docs/results/hallucination.json"
DOCS = {"docs/hallucination-controls.md": ("HALLU_LADDER", "HALLU_SWEEPS"), "README.md": ("HALLU_README",)}


def pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def blocks(r: dict) -> dict:
    rows = ["| Step | Wrong answers reaching customers | Held back (abstain + review) | Answerable held back "
            "(false abstain) | Model calls per question |", "| --- | ---: | ---: | ---: | ---: |"]
    for x in r["ladder"]:
        rows.append(f"| {x['config']} | {pct(x['wrong_to_customer'])} ({x['wrong_to_customer_count']}) | "
                    f"{pct(x['held_back_rate'])} | {pct(x['false_abstain_rate'])} | x{x['cost_multiplier']:g} |")
    sw = ["Retrieval score threshold (control alone; 0 = off):", "",
          "| Min score | Wrong to customer | Held back | False abstain |", "| ---: | ---: | ---: | ---: |"]
    sw += [f"| {s['min_score']} | {pct(s['wrong_to_customer'])} | {pct(s['held_back_rate'])} | "
           f"{pct(s['false_abstain_rate'])} |" for s in r["min_score_sweep"]]
    sw += ["", "Log-prob cutoff on the least confident token (control alone; off = no gate):", "",
           "| Cutoff | Wrong to customer | Held back | False abstain |", "| ---: | ---: | ---: | ---: |"]
    sw += [f"| {'off' if s['cutoff'] is None else s['cutoff']} | {pct(s['wrong_to_customer'])} | "
           f"{pct(s['held_back_rate'])} | {pct(s['false_abstain_rate'])} |" for s in r["logprob_sweep"]]
    sw += ["", "Self-consistency (control alone):", "",
           "| Samples N | Must agree | Wrong to customer | False abstain | Cost |", "| ---: | ---: | ---: | ---: | ---: |"]
    sw += [f"| {s['n']} | {s['min_agree']} | {pct(s['wrong_to_customer'])} | {pct(s['false_abstain_rate'])} | "
           f"x{s['cost_multiplier']:g} |" for s in r["self_consistency_sweep"]]
    lad = r["ladder"]
    qs = r["question_set"]
    n = lad[0]["trials"]
    readme = (f"Simulated (seeded fake model with log-probs), exact counts over {n} trials "
              f"({sum(qs.values())} labelled questions x {r['seeds']} seeds: {qs['answerable']} answerable, "
              f"{qs['unanswerable']} unanswerable, {qs['stale']} stale). Wrong answers reaching customers: "
              f"{pct(lad[0]['wrong_to_customer'])} in the incident setup, {pct(lad[1]['wrong_to_customer'])} with "
              f"the talk 18 fixes, {pct(lad[5]['wrong_to_customer'])} with all five new controls at one model call, "
              f"{pct(lad[7]['wrong_to_customer'])} adding self-consistency only in the log-prob grey band "
              f"(x{lad[7]['cost_multiplier']:g} calls per question vs x{lad[6]['cost_multiplier']:g} for N=5 on "
              f"every question). The price: answerable questions held back rise from "
              f"{pct(lad[1]['false_abstain_rate'])} to {pct(lad[7]['false_abstain_rate'])}.")
    return {"HALLU_LADDER": "\n".join(rows), "HALLU_SWEEPS": "\n".join(sw), "HALLU_README": readme}


def render_docs(r: dict, write: bool) -> list[str]:
    b = blocks(r)
    stale = []
    for f, keys in DOCS.items():
        p = ROOT / f
        old = p.read_text()
        new = old
        for k in keys:
            if f"<!-- BEGIN {k} -->" not in old:
                stale.append(f"{f}: missing block {k}")
            new = re.sub(rf"(<!-- BEGIN {k} -->).*?(<!-- END {k} -->)",
                         lambda m, v=b[k]: m.group(1) + "\n" + v + "\n" + m.group(2), new, flags=re.S)
        if new != old:
            stale.append(f"{f} hallucination numbers are stale")
            if write:
                p.write_text(new)
    return stale


def render() -> str:
    return json.dumps(results(), indent=2) + "\n"


def main(argv) -> int:
    js = render()
    r = json.loads(js)
    if "--check" in argv:
        bad = render_docs(r, write=False)
        if not OUT.exists() or OUT.read_text() != js:
            bad.append("docs/results/hallucination.json differs from a fresh run")
        if bad:
            print("drift: " + "; ".join(bad) + " - run python scripts/hallucination_bench.py")
            return 1
        print("hallucination results and doc tables up to date")
        return 0
    OUT.write_text(js)
    render_docs(r, write=True)
    print(f"wrote {OUT.relative_to(ROOT)} and the doc tables")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
