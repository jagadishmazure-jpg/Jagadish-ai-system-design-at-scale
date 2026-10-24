"""Talk 15 benchmark: writes docs/results/caching.json and docs/img/cost_vs_hit_rate.svg.

--check regenerates both in memory and fails if the committed files drift.
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from sysdesign.ext.llm_cache import curve_svg, run_all  # noqa: E402

JSON = ROOT / "docs/results/caching.json"
SVG = ROOT / "docs/img/cost_vs_hit_rate.svg"


def main(argv) -> int:
    res = run_all()
    js = json.dumps(res, indent=2) + "\n"
    svg = curve_svg([(p["hit_rate"], p["cost_usd"]) for p in res["cost_curve"]])
    if "--check" in argv:
        bad = [str(f.relative_to(ROOT)) for f, new in ((JSON, js), (SVG, svg))
               if not f.exists() or f.read_text() != new]
        if bad:
            print("drift:", ", ".join(bad), "- run python scripts/caching_bench.py")
            return 1
        print("caching results up to date")
        return 0
    JSON.write_text(js)
    SVG.write_text(svg)
    i, f = res["incident"], res["incident"]["fix"]
    print(f"bill {i['bill_growth']}x vs traffic {i['traffic_growth']}x; input cost -{f['input_cost_reduction']:.1%}; "
          f"monthly ${f['monthly_bill_before']:,.0f} -> ${f['monthly_bill_after']:,.0f}; "
          f"TTFT p50 {f['ttft_p50_before']}s -> {f['ttft_p50_after']}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
