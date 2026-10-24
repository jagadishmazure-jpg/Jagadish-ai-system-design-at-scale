"""Talk 18 (A): silent wrong answers. Versioned policies, freshness check, answer verifier, review queue,
and two dashboards (ops vs quality) for a simulated replay.

Story (fictional): the support bot for the made-up Larkspur Home store told a customer on day 45 that the
refund window was 60 days. The window had been cut to 30 days last quarter. Latency, errors and uptime were
all green, because none of them looks at what the answer says.

Everything here is deterministic (seeded) and offline. The "model" is a fake that answers from whatever
retrieval hands it, plus a seeded share of invented numbers and echoed contact details, so the checks have
something real to catch. Numbers are from the simulation, not from a production system.
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from datetime import date

from sysdesign.ext.grounding import check as grounded
from sysdesign.pii import contains_pii, redact

# --------------------------------------------------------------------------- versioned policy store


@dataclass(frozen=True)
class PolicyVersion:
    doc_id: str
    version: int
    effective_from: date
    superseded_on: date | None  # None = still in force
    text: str
    rules: dict  # machine-readable facts, e.g. {"refund_window_days": 30}

    def in_force(self, on: date) -> bool:
        return self.effective_from <= on and (self.superseded_on is None or on < self.superseded_on)


POLICIES = [
    PolicyVersion("refund-policy", 1, date(2025, 1, 1), date(2026, 7, 1),
                  "Refunds: items can be returned for a refund within 60 days of delivery.",
                  {"refund_window_days": 60}),
    PolicyVersion("refund-policy", 2, date(2026, 7, 1), None,
                  "Refunds: items can be returned for a refund within 30 days of delivery.",
                  {"refund_window_days": 30}),
    PolicyVersion("shipping-policy", 1, date(2025, 1, 1), None,
                  "Shipping: standard delivery takes 5 business days; express takes 2 business days.",
                  {"standard_days": 5, "express_days": 2}),
    PolicyVersion("warranty-policy", 1, date(2025, 1, 1), date(2026, 4, 1),
                  "Warranty: furniture is covered for 1 year against defects.", {"warranty_years": 1}),
    PolicyVersion("warranty-policy", 2, date(2026, 4, 1), None,
                  "Warranty: furniture is covered for 2 years against defects.", {"warranty_years": 2}),
]

# which rule a question is about, and how an answer states it
RULE_PATTERNS = {
    "refund_window_days": re.compile(r"(\d+)[- ]day(?:s)?\b[^.]*\b(?:refund|return)|(?:refund|return)[^.]*?\b(\d+)"
                                     r"[- ]days?", re.I),
    "warranty_years": re.compile(r"(\d+)[- ]years?", re.I),
    "standard_days": re.compile(r"standard[^.]*?(\d+)\s*business days", re.I),
}
STOP = {"the", "a", "an", "is", "for", "of", "to", "can", "i", "my", "what", "how", "long", "do", "you", "it",
        "on", "within", "after", "days", "day"}


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", s.lower()) if w not in STOP}


class PolicyStore:
    def __init__(self, versions=POLICIES):
        self.versions = list(versions)

    def search(self, query: str, on: date, exclude_superseded: bool = True, k: int = 1) -> list[PolicyVersion]:
        """Keyword retrieval. With exclude_superseded=False this behaves like the incident index, which kept
        every version and ranked old and new copies as equals (ties go to the older, longer-lived one)."""
        q = _words(query)
        pool = [v for v in self.versions if v.in_force(on)] if exclude_superseded else list(self.versions)
        scored = [(len(q & _words(v.text)), -v.version, v) for v in pool]
        scored = [s for s in scored if s[0] > 0]
        scored.sort(key=lambda s: (-s[0], -s[1]))  # equal score: lower version first (the incident bug)
        return [s[2] for s in scored[:k]]

    def current(self, doc_id: str, on: date) -> PolicyVersion:
        return next(v for v in self.versions if v.doc_id == doc_id and v.in_force(on))

    def superseded_values(self, rule: str, on: date) -> set:
        return {v.rules[rule] for v in self.versions if rule in v.rules and not v.in_force(on)
                and v.effective_from <= on}

    def current_value(self, rule: str, on: date):
        for v in self.versions:
            if rule in v.rules and v.in_force(on):
                return v.rules[rule]
        return None


# --------------------------------------------------------------------------- freshness check


def claimed_values(answer: str) -> dict:
    out = {}
    for rule, pat in RULE_PATTERNS.items():
        for m in pat.finditer(answer):
            val = next(g for g in m.groups() if g)
            out.setdefault(rule, set()).add(int(val))
    return out


def freshness_check(answer: str, store: PolicyStore, on: date) -> dict:
    """Flag an answer that states a value only found in a superseded policy version."""
    stale = []
    for rule, vals in claimed_values(answer).items():
        cur = store.current_value(rule, on)
        old = store.superseded_values(rule, on)
        for v in sorted(vals):
            if v != cur and v in old:
                stale.append({"rule": rule, "claimed": v, "current": cur})
    return {"fresh": not stale, "stale": stale}


# --------------------------------------------------------------------------- verifier + review queue


@dataclass
class Verdict:
    groundedness: float  # share of claimed values found in the evidence handed to the model
    fresh: bool
    safe: bool
    confidence: float
    route: str  # "send" or "review"
    reasons: list = field(default_factory=list)


def verify(answer: str, evidence: list[str], store: PolicyStore, on: date, threshold: float = 0.8) -> Verdict:
    g = grounded(answer, evidence)  # reuse the talk 3 grounding check (numbers must come from evidence)
    nums = set(re.findall(r"(?<![\w.-])\d+(?:\.\d+)?(?![\w-])", answer))
    grounded_share = 1.0 if not nums else 1 - len(g["ungrounded"]) / max(len(nums), len(g["ungrounded"]))
    f = freshness_check(answer, store, on)
    safe = not contains_pii(answer)
    reasons = []
    if not g["grounded"]:
        reasons.append("ungrounded:" + ",".join(g["ungrounded"]))
    if not f["fresh"]:
        reasons.append("stale:" + ",".join(f"{s['rule']}={s['claimed']}" for s in f["stale"]))
    if not safe:
        reasons.append("pii")
    conf = grounded_share * (1.0 if f["fresh"] else 0.3) * (1.0 if safe else 0.2)
    route = "send" if conf >= threshold else "review"
    return Verdict(round(grounded_share, 3), f["fresh"], safe, round(conf, 3), route, reasons)


class ReviewQueue:
    """Doubtful answers wait here for a specialist. Stored redacted, never sent until a person approves."""

    def __init__(self):
        self.items: list[dict] = []

    def put(self, question: str, answer: str, verdict: Verdict) -> int:
        self.items.append({"id": len(self.items) + 1, "question": redact(question), "draft": redact(answer),
                           "reasons": verdict.reasons, "status": "pending"})
        return len(self.items)

    def approve(self, item_id: int, final: str) -> dict:
        it = self.items[item_id - 1]
        it.update(status="approved", final=redact(final))
        return it


# --------------------------------------------------------------------------- replay + two dashboards

QUESTIONS = [
    ("Can I return my sofa for a refund? It arrived 45 days ago.", "refund_window_days"),
    ("What is the refund window for returns?", "refund_window_days"),
    ("How long is the warranty on furniture defects?", "warranty_years"),
    ("How many business days does standard shipping take?", "standard_days"),
]
TODAY = date(2026, 10, 24)


def fake_model(question: str, docs: list[PolicyVersion], rng: random.Random, hallucinate: float,
               echo_pii: float) -> str:
    """Answers from the top document. A seeded share of answers invents a number or echoes contact details,
    which is how real models fail silently: a fluent, confident 200 OK."""
    if not docs:
        return "I'm not sure; let me connect you with the team."
    d = docs[0]
    ans = f"Per our policy: {d.text.split(': ', 1)[1]}"
    if rng.random() < hallucinate:
        ans += " You also get a 15% restocking credit."  # made-up fact, not in any document
    if rng.random() < echo_pii:
        ans += " We will email you at sam.rowe@example.com."  # model echoed contact details
    return ans


def replay(n: int = 400, filter_superseded: bool = False, verifier: bool = False, seed: int = 18) -> dict:
    rng = random.Random(seed)
    store = PolicyStore()
    queue = ReviewQueue()
    lat, errors, sent_wrong = [], 0, {"veracity": 0, "freshness": 0, "safety": 0}
    caught = {"veracity": 0, "freshness": 0, "safety": 0}
    truth_wrong = {"veracity": 0, "freshness": 0, "safety": 0}
    reviewed = 0
    for _ in range(n):
        q, rule = QUESTIONS[rng.randrange(len(QUESTIONS))]
        docs = store.search(q, TODAY, exclude_superseded=filter_superseded)
        ans = fake_model(q, docs, rng, hallucinate=0.06, echo_pii=0.03)
        lat.append(round(0.6 + rng.random() * 0.5, 3))  # simulated seconds; the same in both runs
        ev = [d.text for d in docs]
        # ground truth, judged against the CURRENT policy, independent of the verifier
        is_stale = not freshness_check(ans, store, TODAY)["fresh"]
        invented = "15%" in ans
        leaked = contains_pii(ans)
        for k, bad in (("freshness", is_stale), ("veracity", invented), ("safety", leaked)):
            truth_wrong[k] += bad
        if verifier:
            v = verify(ans, ev, store, TODAY)
            if v.route == "review":
                reviewed += 1
                queue.put(q, ans, v)
                for k, bad in (("freshness", is_stale), ("veracity", invented), ("safety", leaked)):
                    caught[k] += bad
                continue
        for k, bad in (("freshness", is_stale), ("veracity", invented), ("safety", leaked)):
            sent_wrong[k] += bad
    lat.sort()
    ops = {"p95_latency_s": lat[int(0.95 * len(lat)) - 1], "error_rate": errors / n, "uptime": 1.0,
           "http_200_share": 1.0}
    quality = {k: round(1 - sent_wrong[k] / n, 4) for k in sent_wrong}
    return {"n": n, "filter_superseded": filter_superseded, "verifier": verifier, "ops": ops, "quality_score_sent": quality, "wrong_sent": sent_wrong,
            "wrong_generated": truth_wrong, "caught_by_verifier": caught, "sent_to_review": reviewed,
            "review_queue_len": len(queue.items),
            "ops_dashboard": "green" if ops["error_rate"] < 0.01 and ops["p95_latency_s"] < 2 else "red",
            "quality_dashboard": "green" if all(v == 0 for v in sent_wrong.values()) else "red"}


def results() -> dict:
    return {"note": "simulated replay, seeded; fictional store and policies", "before": replay(),
            "verifier_only": replay(verifier=True),
            "after": replay(filter_superseded=True, verifier=True)}
