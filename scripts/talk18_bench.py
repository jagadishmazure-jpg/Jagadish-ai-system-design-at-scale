"""Talk 18 numbers.

python scripts/talk18_bench.py            -> docs/results/talk18.json (deterministic: freshness replay + waterfall
                                             model). --check fails on drift; a test runs the same check.
python scripts/talk18_bench.py --measure  -> also docs/results/talk18_measured.json (real timings on this
                                             machine: concurrency replay, experiments, delay-proxy TLS probes).
                                             Not drift-checked: timings differ per machine and run.
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from sysdesign.ext import freshness, waterfall  # noqa: E402

OUT = ROOT / "docs/results/talk18.json"
MEASURED = ROOT / "docs/results/talk18_measured.json"


def deterministic() -> str:
    return json.dumps({"freshness": freshness.results(), "waterfall": waterfall.results()}, indent=2) + "\n"


def measured() -> dict:
    from sysdesign.ext import concurrency
    from tools.delay_proxy import measure
    out = concurrency.results()
    out["delay_proxy_rtt_100ms"] = {v: measure(100, v) for v in ("1.2", "1.3")}
    return out


def main(argv) -> int:
    js = deterministic()
    if "--check" in argv:
        if not OUT.exists() or OUT.read_text() != js:
            print("drift: docs/results/talk18.json - run python scripts/talk18_bench.py")
            return 1
        print("talk 18 results up to date")
        return 0
    OUT.write_text(js)
    print(f"wrote {OUT.relative_to(ROOT)}")
    if "--measure" in argv:
        MEASURED.write_text(json.dumps(measured(), indent=2) + "\n")
        print(f"wrote {MEASURED.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
