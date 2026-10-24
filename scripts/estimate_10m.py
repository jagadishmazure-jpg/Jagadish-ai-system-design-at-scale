"""Back-of-envelope capacity and cost estimate (illustrative example prices, not a quote).

python -m scripts.estimate_10m            # baseline 10M users
python -m scripts.estimate_10m --scale 10 # same inputs at 10x users
"""
import argparse
import json

from sysdesign.ext.cost_model import report

ap = argparse.ArgumentParser()
ap.add_argument("--users", type=float, default=10_000_000)
ap.add_argument("--scale", type=float, default=1.0)
a = ap.parse_args()
print(json.dumps(report(users=a.users * a.scale), indent=2, default=str))
