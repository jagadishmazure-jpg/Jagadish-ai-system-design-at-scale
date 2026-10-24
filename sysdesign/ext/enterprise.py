"""Enterprise controls: ingestion poisoning checks, model-extraction detection, right to be
forgotten in RAG, minimum-necessary redaction, cost tags, TCO and budget alerts."""
from __future__ import annotations

import math
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field

from ..guardrails import check_input
from ..pii import redact


def is_instruction_like(text: str) -> bool:
    r = check_input(text)
    return not r.allowed and r.reason.startswith("injection")

# ---------------------------------------------------------------- data poisoning
TRUSTED_SOURCES = {"policy-cms", "legal-review"}


def poisoning_check(doc: dict, existing: list[dict]) -> list[str]:
    """Reject docs from unknown sources, with embedded instructions, hidden text, or that silently
    contradict a numeric policy value (e.g. return window 30 -> 365 days) without review."""
    issues = []
    if doc.get("source") not in TRUSTED_SOURCES:
        issues.append("untrusted_source")
    if is_instruction_like(doc["text"]):
        issues.append("embedded_instructions")
    if re.search(r"[\u200b-\u200f\u202a-\u202e]", doc["text"]):
        issues.append("hidden_characters")
    for old in existing:
        if old["id"] == doc["id"] and not doc.get("reviewed"):
            on = re.findall(r"\d+", old["text"])
            nn = re.findall(r"\d+", doc["text"])
            if on != nn:
                issues.append("unreviewed_numeric_change")
    return issues


# ---------------------------------------------------------------- model extraction / theft
@dataclass
class ExtractionDetector:
    """Flags callers whose traffic looks like systematic probing: high volume, mostly unique
    templated prompts, many near-duplicates differing by one token."""
    window: int = 200
    max_rate: int = 120
    hist: dict = field(default_factory=lambda: defaultdict(lambda: deque(maxlen=200)))

    def observe(self, user: str, prompt: str) -> dict:
        h = self.hist[user]
        h.append(prompt)
        n = len(h)
        if n < 30:
            return {"suspicious": False, "n": n}
        skeletons = [re.sub(r"\d+|\b\w{1,3}\b", "#", p) for p in h]
        top_share = max(skeletons.count(s) for s in set(skeletons)) / n
        uniq = len(set(h)) / n
        suspicious = (n >= self.max_rate and uniq > 0.9) or (top_share > 0.6 and uniq > 0.9)
        return {"suspicious": suspicious, "n": n, "template_share": round(top_share, 2), "unique": round(uniq, 2)}


# ---------------------------------------------------------------- right to be forgotten
def forget_user(kb, user_id: str, cache=None) -> dict:
    """Delete every chunk owned by the user (CDC delete events) and drop dependent cache entries."""
    ids = [cid for cid, c in kb.chunks.items() if cid.startswith(f"user:{user_id}:")]
    changed = kb.apply_changes([{"op": "delete", "id": i} for i in ids])
    dropped = cache.invalidate_chunks(changed) if cache is not None else 0
    return {"deleted": changed, "cache_dropped": dropped}


# ---------------------------------------------------------------- minimum necessary
MEDICAL = re.compile(r"\b(diagnos\w*|prescription|allergy|allergic|pregnan\w*|disability|medication)\b", re.IGNORECASE)


def minimum_necessary(text: str, purpose: str) -> str:
    """Before any model call: strip direct identifiers always; strip health details unless the purpose
    actually needs them (only 'accessibility_request' does)."""
    out = redact(text)
    if purpose != "accessibility_request":
        out = MEDICAL.sub("[HEALTH]", out)
    return out


# ---------------------------------------------------------------- FinOps
@dataclass
class CostLedger:
    entries: list = field(default_factory=list)

    def tag(self, request_id: str, tenant: str, feature: str, tier: str, cost_usd: float) -> None:
        if not isinstance(cost_usd, (int, float)) or math.isnan(cost_usd) or cost_usd < 0:
            raise ValueError("cost must be a non-negative number")
        self.entries.append({"request_id": request_id, "tenant": tenant, "feature": feature, "tier": tier,
                             "cost_usd": cost_usd})

    def by(self, key: str) -> dict:
        out: dict = defaultdict(float)
        for e in self.entries:
            out[e[key]] += e["cost_usd"]
        return {k: round(v, 6) for k, v in sorted(out.items())}


def budget_alert(spent: float, budget: float, warn_at: float = 0.8) -> str:
    for name, v in (("spent", spent), ("budget", budget), ("warn_at", warn_at)):
        if not isinstance(v, (int, float)) or math.isnan(v) or v < 0:
            raise ValueError(f"{name} must be >= 0")
    if budget == 0:
        return "over" if spent > 0 else "ok"
    r = spent / budget
    return "over" if r >= 1 else "warn" if r >= warn_at else "ok"


def tco(monthly_requests: float, cost_per_request: float, engineers_fte: float = 1.0, fte_month: float = 12_000,
        infra_month: float = 1_500, eval_month: float = 300) -> dict:
    """Example inputs only. Shows that people and infra usually dwarf token spend at small scale."""
    for v in (monthly_requests, cost_per_request, engineers_fte, fte_month, infra_month, eval_month):
        if v < 0:
            raise ValueError("inputs must be >= 0")
    inference = monthly_requests * cost_per_request
    lines = {"inference": inference, "infrastructure": infra_month, "evaluation_and_monitoring": eval_month,
             "engineering": engineers_fte * fte_month}
    total = sum(lines.values())
    return {"lines": {k: round(v, 2) for k, v in lines.items()}, "total_month": round(total, 2),
            "share_inference": round(inference / total, 4) if total else 0.0}


def isolate(chunks: list[dict]) -> tuple[list[dict], list[str]]:
    """Return (safe chunks marked as untrusted data, quarantined chunk ids)."""
    safe, quarantined = [], []
    for c in chunks:
        if is_instruction_like(c["text"]):
            quarantined.append(c["id"])
        else:
            safe.append({**c, "trust": "untrusted-data"})
    return safe, quarantined
