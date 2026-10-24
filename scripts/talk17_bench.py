"""Talk 17 deterministic numbers: writes docs/results/talk17.json. --check fails on drift.

Covers streaming arithmetic, retry budget and amplification, mean vs percentiles, partial pages and
stateless replicas. The wall-clock stream replay is separate: scripts/stream_replay.py.
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from sysdesign.ext import retries, stateless, streaming, tail_extras  # noqa: E402

OUT = ROOT / "docs/results/talk17.json"


def results() -> dict:
    return {"streaming": streaming.results(), "retries": retries.results(),
            "tail_extras": tail_extras.results(), "stateless": stateless.results()}


def main(argv) -> int:
    js = json.dumps(results(), indent=2) + "\n"
    if "--check" in argv:
        if not OUT.exists() or OUT.read_text() != js:
            print("drift: docs/results/talk17.json - run python scripts/talk17_bench.py")
            return 1
        print("talk 17 results up to date")
        return 0
    OUT.write_text(js)
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
