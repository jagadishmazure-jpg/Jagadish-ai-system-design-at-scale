"""Print the capacity estimate as JSON. Override inputs: python scripts/capacity.py users=50000 peak_multiplier=20"""
import json
import pathlib
import sys
from dataclasses import fields

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from sysdesign.capacity import Inputs, compute  # noqa: E402

kw = {}
types = {f.name: f.type for f in fields(Inputs)}
for arg in sys.argv[1:]:
    k, _, v = arg.partition("=")
    if k not in types:
        sys.exit(f"unknown input {k}")
    kw[k] = int(v) if types[k] in (int, "int") else float(v)
print(json.dumps(compute(Inputs(**kw)), indent=2))
