"""Grounding check: every number, money amount, date and order status in a final answer must
appear in a tool result (or retrieved chunk) from THIS run. Ungrounded values block the answer."""
from __future__ import annotations

import json
import re

MONEY = re.compile(r"\$\s?(\d+(?:,\d{3})*(?:\.\d{1,2})?)")
NUM = re.compile(r"(?<![\w.-])(\d+(?:\.\d+)?)(?![\w-])")
DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
STATUSES = {"delivered", "in_transit", "in transit", "processing", "returned", "cancelled", "shipped"}


def _values(text: str) -> set[str]:
    vals = {m.group(1).replace(",", "") for m in MONEY.finditer(text)}
    vals |= {m.group(0) for m in DATE.finditer(text)}
    for m in NUM.finditer(text):
        vals.add(m.group(1))
    return {_norm(v) for v in vals}


def _norm(v: str) -> str:
    try:
        f = float(v)
        return f"{f:.2f}"
    except ValueError:
        return v


def check(answer: str, evidence: list) -> dict:
    ev_text = " ".join(e if isinstance(e, str) else json.dumps(e) for e in evidence)
    allowed = _values(ev_text)
    claimed = _values(answer)
    # order ids are entities, not quantities: strip them before checking numbers
    for oid in re.findall(r"KG-(\d{5})", answer):
        claimed.discard(_norm(oid))
    ungrounded = sorted(claimed - allowed)
    a, e = answer.lower(), ev_text.lower()
    bad_status = sorted(s for s in STATUSES if re.search(rf"\b{s}\b", a) and s.replace(" ", "_") not in e
                        and s not in e)
    return {"grounded": not ungrounded and not bad_status, "ungrounded": ungrounded + bad_status}
