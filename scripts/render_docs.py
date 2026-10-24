"""Regenerate the numbers in README.md and docs/capacity.md from real runs; --check fails on drift.

Sources: loadtest/results/results.json (committed load model output),
sysdesign.capacity.compute(defaults), and scripts/count_tests.py.
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from sysdesign.capacity import Inputs, compute  # noqa: E402

RES = json.loads((ROOT / "loadtest/results/results.json").read_text())


def pct(x):
    return f"{100 * x:.1f}%"


def sec(x):
    return "n/a" if x is None else f"{x:.3f}s"


def usd(x):
    return f"${x:.4f}"


def test_count() -> int:
    out = subprocess.run([sys.executable, str(ROOT / "scripts/count_tests.py")], capture_output=True, text=True,
                         check=True, cwd=ROOT).stdout
    return int(re.search(r"(\d+) tests collected", out).group(1))


def results_table() -> str:
    rl, sc, rt = RES["rate_limiting"], RES["semantic_cache"], RES["intent_router"]
    po, rf, qa = RES["provider_outage"], RES["replica_failure"], RES["queue_and_autoscaling"]
    rows = [
        ("Rate limiting (normal users while one user floods)", "p95 / error rate",
         f"{sec(rl['off']['normal_users']['p95_s'])} / {pct(rl['off']['normal_users']['error_rate'])}",
         f"{sec(rl['on']['normal_users']['p95_s'])} / {pct(rl['on']['normal_users']['error_rate'])}"),
        ("Rate limiting (the flooding user)", "429 rate", pct(rl["off"]["abuser"]["rate_429"]),
         pct(rl["on"]["abuser"]["rate_429"])),
        ("Semantic cache (policy questions)", "hit rate / cost per 1k / p50",
         f"{pct(sc['off']['cache_hit_rate'])} / {usd(sc['off']['cost_per_1k_requests_usd'])} / {sec(sc['off']['p50_s'])}",
         f"{pct(sc['on']['cache_hit_rate'])} / {usd(sc['on']['cost_per_1k_requests_usd'])} / {sec(sc['on']['p50_s'])}"),
        ("Semantic cache correctness", "hits citing different sources than fresh retrieval", "n/a",
         f"{sc['on']['wrong_source_hits']} of {sc['on']['cache_hits']}"),
        ("Intent router ladder (mixed traffic)", "cost per 1k / p95",
         f"{usd(rt['off_all_large_model']['cost_per_1k_requests_usd'])} / {sec(rt['off_all_large_model']['p95_s'])}",
         f"{usd(rt['on_ladder']['cost_per_1k_requests_usd'])} / {sec(rt['on_ladder']['p95_s'])}"),
        ("Breaker + fallback (primary down 60s of 180s)", "error rate during outage / disrupted seconds",
         f"{pct(po['off_naive_retries']['outage_window']['error_rate'])} / {po['off_naive_retries']['disrupted_seconds']}",
         f"{pct(po['on_breaker_fallback']['outage_window']['error_rate'])} / "
         f"{po['on_breaker_fallback']['disrupted_seconds']}"),
        ("Breaker + fallback", "calls sent to the failing primary", str(po["off_naive_retries"]["primary_calls"]),
         str(po["on_breaker_fallback"]["primary_calls"])),
        ("Health-checked load balancer (1 of 3 replicas dies at 30s)", "error rate",
         pct(rf["off_no_health_checks"]["error_rate"]), pct(rf["on_health_checked_lb"]["error_rate"])),
        ("Bulkheads + autoscaler (bulk burst + interactive jobs)", "interactive wait p95",
         sec(qa["off_shared_fixed_2"]["interactive_wait_p95_s"]),
         sec(qa["on_bulkheads_autoscaled"]["interactive_wait_p95_s"])),
        ("Bulkheads + autoscaler", "bulk backlog drain time", sec(qa["off_shared_fixed_2"]["bulk_drain_time_s"]),
         sec(qa["on_bulkheads_autoscaled"]["bulk_drain_time_s"])),
    ]
    head = "| Pattern | Metric | Off | On |\n| --- | --- | ---: | ---: |\n"
    return head + "\n".join(f"| {a} | {b} | {c} | {d} |" for a, b, c, d in rows)


def paths_table() -> str:
    p = RES["intent_router"]["on_ladder"]["paths"]
    rows = "\n".join(f"| {k} | {v['requests']} | {usd(v['cost_per_request_usd'] * 1000)} |" for k, v in p.items())
    return "| Ladder rung | Requests | Cost per 1k requests |\n| --- | ---: | ---: |\n" + rows


def capacity_table() -> str:
    c = compute(Inputs())
    rows = [("Requests per day", f"{c['requests_per_day']:,}"), ("Average RPS", f"{c['avg_rps']}"),
            ("Peak RPS (x{:g})".format(c["inputs"]["peak_multiplier"]), f"{c['peak_rps']}"),
            ("Peak concurrent requests (Little's law)", f"{c['peak_concurrent_requests']}"),
            ("LLM calls per month", f"{c['llm_calls_per_month']:,}"),
            ("Tokens per month", f"{c['tokens_per_month']:,}"),
            ("LLM spend per month (ladder + cache)", f"${c['llm_cost_per_month_usd']:,.2f}"),
            ("LLM spend per month (everything on the large model)", f"${c['all_large_model_cost_per_month_usd']:,.2f}"),
            ("Allowed downtime per month at {:g}%".format(100 * c["inputs"]["uptime_target"]),
             f"{c['allowed_downtime_min_per_month']} min")]
    return "| Quantity | Value |\n| --- | ---: |\n" + "\n".join(f"| {a} | {b} |" for a, b in rows)


def blocks() -> dict:
    n = test_count()
    rl, sc, rt, po = RES["rate_limiting"], RES["semantic_cache"], RES["intent_router"], RES["provider_outage"]
    ratio = rt["off_all_large_model"]["cost_usd"] / rt["on_ladder"]["cost_usd"]
    summary = (f"**{n} tests.** In the load model (virtual time, fake providers): rate limiting kept normal users' "
               f"p95 at {sec(rl['on']['normal_users']['p95_s'])} versus {sec(rl['off']['normal_users']['p95_s'])} "
               f"and errors at {pct(rl['on']['normal_users']['error_rate'])} versus "
               f"{pct(rl['off']['normal_users']['error_rate'])} while one user flooded; the intent ladder cut "
               f"model spend {ratio:.1f}x; breaker + fallback took outage-window errors from "
               f"{pct(po['off_naive_retries']['outage_window']['error_rate'])} to "
               f"{pct(po['on_breaker_fallback']['outage_window']['error_rate'])}; the semantic cache served "
               f"{pct(sc['on']['cache_hit_rate'])} of policy questions with "
               f"{sc['on']['wrong_source_hits']} wrong-source hits.")
    return {"SUMMARY": summary, "RESULTS": results_table(), "PATHS": paths_table(), "CAPACITY": capacity_table(),
            "TESTCOUNT": f"{n} tests collected"}


def render(text: str, b: dict) -> str:
    for k, v in b.items():
        text = re.sub(rf"(<!-- BEGIN {k} -->).*?(<!-- END {k} -->)",
                      lambda m, v=v: m.group(1) + "\n" + v + "\n" + m.group(2),
                      text, flags=re.S)
    return text


def main(argv) -> int:
    b = blocks()
    bad = []
    for f in ("README.md", "docs/capacity.md"):
        p = ROOT / f
        old = p.read_text()
        new = render(old, b)
        for k in b:
            if f == "README.md" and k != "CAPACITY" and f"<!-- BEGIN {k} -->" not in old:
                bad.append(f"{f}: missing block {k}")
        if new != old:
            if "--check" in argv:
                bad.append(f"{f} numbers are stale; run python scripts/render_docs.py")
            else:
                p.write_text(new)
    if bad:
        print("\n".join(bad))
        return 1
    print("docs match the committed results and a fresh test count")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
