"""Talk 16 numbers: writes docs/results/tail_latency.json. --check fails on drift."""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from sysdesign.ext.tail_latency import results  # noqa: E402

OUT = ROOT / "docs/results/tail_latency.json"


def main(argv) -> int:
    js = json.dumps(results(), indent=2) + "\n"
    if "--check" in argv:
        if not OUT.exists() or OUT.read_text() != js:
            print("drift: docs/results/tail_latency.json - run python scripts/tail_latency_bench.py")
            return 1
        print("tail latency results up to date")
        return 0
    OUT.write_text(js)
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
