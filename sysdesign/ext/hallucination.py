"""Hallucination controls on top of the talk 18 stack.

Talk 18 already has dated retrieval, a grounding + freshness + PII verifier and a review queue
(`ext/freshness.py`). This module adds the five controls that were missing:

1. ``logprob_gate``      - hold an answer back when the model was unsure of the tokens it chose.
2. ``self_consistency``  - sample N answers, keep the majority only if enough samples agree, else abstain.
3. ``retrieve`` + ``min_score`` - drop weak chunks; with nothing left, abstain instead of guessing.
4. ``enforce_citations`` - every answer must cite chunk ids that exist, are in force today, and support it.
5. ``IDK_PROMPT`` + ``validate_output`` - "answer only from context, else say you don't know" with
   few-shot refusal examples, and a strict JSON shape (no extra keys, typed fields).

The model is a seeded fake (``FakeLM``) that exposes per-token log-probs. Its failure rates are invented,
so every number produced here is from a simulation, measured exactly on a labelled question set. The store
and its policies are the fictional ones from talk 18.
"""
from __future__ import annotations

import json
import random
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

from sysdesign.ext.freshness import TODAY, PolicyStore, ReviewQueue, claimed_values, verify
from sysdesign.pii import redact

# --------------------------------------------------------------------------- labelled question set
# kind: answerable (truth = current value), unanswerable (truth = abstain), stale (asks about an old value).
RULE_FOR_DOC = {"refund-policy": "refund_window_days", "warranty-policy": "warranty_years",
                "shipping-policy": "standard_days"}
QUESTIONS: list[dict] = [
    {"q": "What is the refund window for returns?", "kind": "answerable", "rule": "refund_window_days"},
    {"q": "How many days do I have to return an item for a refund?", "kind": "answerable",
     "rule": "refund_window_days"},
    {"q": "Can I return my sofa for a refund after delivery?", "kind": "answerable", "rule": "refund_window_days"},
    {"q": "Refund deadline for returned items?", "kind": "answerable", "rule": "refund_window_days"},
    {"q": "When does my chance to send it back end?", "kind": "answerable", "rule": "refund_window_days"},
    {"q": "How long is the warranty on furniture defects?", "kind": "answerable", "rule": "warranty_years"},
    {"q": "Is furniture covered against defects, and for how long?", "kind": "answerable",
     "rule": "warranty_years"},
    {"q": "What warranty comes with furniture?", "kind": "answerable", "rule": "warranty_years"},
    {"q": "My table leg cracked, am I still protected?", "kind": "answerable", "rule": "warranty_years"},
    {"q": "How many business days does standard shipping take?", "kind": "answerable", "rule": "standard_days"},
    {"q": "How long does standard delivery take?", "kind": "answerable", "rule": "standard_days"},
    {"q": "Standard shipping time in business days?", "kind": "answerable", "rule": "standard_days"},
    {"q": "When will my parcel show up?", "kind": "answerable", "rule": "standard_days"},
    {"q": "Shipping: how many business days for standard delivery?", "kind": "answerable",
     "rule": "standard_days"},
    {"q": "Do you price match other stores?", "kind": "unanswerable"},
    {"q": "Can I get a refund on a gift card?", "kind": "unanswerable"},
    {"q": "What are your store opening hours?", "kind": "unanswerable"},
    {"q": "How much does international shipping cost?", "kind": "unanswerable"},
    {"q": "Does the warranty cover accidental water damage to electronics?", "kind": "unanswerable"},
    {"q": "How many loyalty points do I earn per order?", "kind": "unanswerable"},
    {"q": "Is there a restocking fee on returns?", "kind": "unanswerable"},
    {"q": "Can I pay for delivery in instalments?", "kind": "unanswerable"},
    {"q": "Do you deliver on Sundays?", "kind": "unanswerable"},
    {"q": "What discount do students get?", "kind": "unanswerable"},
    {"q": "Is the refund window still 60 days for returns?", "kind": "stale", "rule": "refund_window_days"},
    {"q": "Items can be returned for a refund within 60 days, right?", "kind": "stale",
     "rule": "refund_window_days"},
    {"q": "Furniture is covered for 1 year against defects, correct?", "kind": "stale", "rule": "warranty_years"},
    {"q": "Is the furniture warranty 1 year?", "kind": "stale", "rule": "warranty_years"},
    {"q": "My friend said refunds are allowed for 60 days after delivery, true?", "kind": "stale",
     "rule": "refund_window_days"},
    {"q": "Warranty: is furniture still only covered for 1 year?", "kind": "stale", "rule": "warranty_years"},
]

# --------------------------------------------------------------------------- retrieval with scores
STOP = {"the", "a", "an", "is", "for", "of", "to", "can", "i", "my", "what", "how", "long", "do", "you", "it",
        "on", "within", "after", "and", "are", "am", "does", "will", "when", "there", "your", "with", "per",
        "still", "right", "true", "correct", "much", "many", "me", "get", "only", "said", "friend", "comes",
        "time", "send", "back", "end", "chance", "up", "show", "items", "item", "have", "be", "our", "takes"}


def _stem(w: str) -> str:
    if len(w) > 5 and w.endswith("ed"):
        w = w[:-2]
    return w.rstrip("s") if len(w) > 3 else w


def words(s: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[a-z]+", s.lower()) if w not in STOP}


def chunk_id(v) -> str:
    return f"{v.doc_id}@v{v.version}"


def retrieve(question: str, store: PolicyStore, on: date, dated: bool = True, k: int = 2,
             min_score: float = 0.0) -> list[dict]:
    """Score = share of the question's content words found in the chunk (0..1). ``dated`` keeps only
    versions in force on ``on`` (the talk 18 fix); ``min_score`` is control 3."""
    q = words(question)
    pool = [v for v in store.versions if v.in_force(on)] if dated else list(store.versions)
    scored = []
    for v in pool:
        s = len(q & words(v.text)) / max(1, len(q))
        scored.append({"id": chunk_id(v), "text": v.text, "score": round(s, 3), "version": v.version,
                       "doc_id": v.doc_id})
    # ties go to the older version: the incident index bug, kept so the undated run reproduces it
    scored.sort(key=lambda c: (-c["score"], c["version"]))
    return [c for c in scored[:k] if c["score"] > 0 and c["score"] >= min_score]


# --------------------------------------------------------------------------- prompt + output schema
IDK_PROMPT = (
    "Answer only from the numbered context. If the context does not contain the answer, set abstain to true "
    "and say you don't know. Never invent numbers. Cite the chunk ids you used.\n"
    "Example 1. Context: [shipping-policy@v1] standard delivery takes 5 business days. "
    "Question: Do you ship to the moon? -> "
    '{"answer": "I don\'t know; I will pass this to the team.", "citations": [], "abstain": true}\n'
    "Example 2. Context: [shipping-policy@v1] standard delivery takes 5 business days. "
    "Question: How long is standard delivery? -> "
    '{"answer": "Standard delivery takes 5 business days.", "citations": ["shipping-policy@v1"], '
    '"abstain": false}\n'
)
SCHEMA = {"answer": str, "citations": list, "abstain": bool}


def validate_output(raw: str) -> dict | None:
    """Strict shape check: valid JSON object, exactly the schema keys, right types, string citations.
    Returns None on any failure (the caller sends it to review, never to the customer)."""
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(obj, dict) or set(obj) != set(SCHEMA):
        return None
    if not all(isinstance(obj[k], t) for k, t in SCHEMA.items()):
        return None
    if not all(isinstance(c, str) for c in obj["citations"]):
        return None
    return obj


# --------------------------------------------------------------------------- fake model with log-probs
@dataclass
class Generation:
    raw: str                  # what the model emitted (JSON in schema mode, prose otherwise)
    tokens: list[str]
    logprobs: list[float]     # natural-log probability of each emitted token


INVENTED = [14, 21, 45, 90]
# made-up extra claims with no number in them: the talk 18 verifier (numbers only) cannot see these
INVENTED_CLAIMS = ["Assembly is included free.", "We also collect the old item at no charge.",
                   "Exchanges never need a receipt."]


class FakeLM:
    """Deterministic stand-in for a model. Invented failure rates (simulation inputs, not measurements):
    - relevant context: answers from the top chunk; 8% add an invented fact (half with a number, half without) (65% of those with low token
      confidence, 35% confidently); 6% drop the citation; 3% cite a chunk id that does not exist.
    - weak context (top chunk covers < 50% of the question): with the plain prompt it guesses a number 60% of
      the time, else answers from the chunk anyway; with the "I don't know" prompt it abstains 75% of the time.
    - no context: plain prompt guesses; "I don't know" prompt abstains.
    Guessed numbers vary between samples, which is what self-consistency detects.
    """

    calls = 0

    def __init__(self, seed: int = 0):
        self.seed = seed

    def generate(self, question: str, chunks: list[dict], idk_prompt: bool, schema: bool, sample: int = 0
                 ) -> Generation:
        FakeLM.calls += 1
        rng = random.Random(f"{self.seed}|{question}|{sample}|{idk_prompt}")
        top = chunks[0] if chunks else None
        weak = top is None or top["score"] < 0.5
        cites: list[str] = []
        abstain = False
        conf = -0.05  # mean token log-prob for a well-supported answer
        if top is None or (weak and (idk_prompt and rng.random() < 0.75)):
            text, abstain, conf = "I don't know; I will pass this to the team.", True, -0.1
        elif weak and (not idk_prompt or rng.random() < 0.6) and rng.random() < 0.6:
            text = f"Yes, that is allowed within {rng.choice(INVENTED)} days."
            conf = -1.4 if rng.random() < 0.7 else -0.35
        else:
            body = top["text"].split(": ", 1)[1]
            text = f"Per our policy: {body}"
            cites = [top["id"]]
            if rng.random() < 0.08:
                if rng.random() < 0.5:
                    text += f" You also get a {rng.choice((10, 15, 20))}% restocking credit."
                else:
                    text += " " + rng.choice(INVENTED_CLAIMS)
                conf = -2.2 if rng.random() < 0.65 else -0.3
            r = rng.random()
            if r < 0.06:
                cites = []
            elif r < 0.09:
                cites = [top["doc_id"] + "@v9"]
        toks = text.split()
        lps = [round(min(-0.001, conf + rng.gauss(0, 0.02)), 4) for _ in toks]
        if conf < -0.2:  # the uncertain part is the made-up claim: its last tokens carry the low score
            lps = [round(min(-0.001, -0.03 + rng.gauss(0, 0.01)), 4) for _ in toks[:-4]]
            lps += [round(min(-0.001, conf + rng.gauss(0, 0.1)), 4) for _ in toks[-4:]]
        if schema:
            raw = json.dumps({"answer": text, "citations": cites, "abstain": abstain})
        else:
            raw = text + (" [" + ", ".join(cites) + "]" if cites else "")
        return Generation(raw, toks, lps)


def parse(gen: Generation, schema: bool) -> dict | None:
    if schema:
        return validate_output(gen.raw)
    m = re.search(r"\[([^\]]+)\]$", gen.raw)
    cites = [c.strip() for c in m.group(1).split(",")] if m else []
    text = gen.raw[: m.start()].strip() if m else gen.raw
    return {"answer": text, "citations": cites, "abstain": text.startswith("I don't know")}


# --------------------------------------------------------------------------- controls 1, 2, 4
def logprob_gate(gen: Generation, cutoff: float) -> bool:
    """True = confident enough to send. Uses the least confident token, since one invented number is enough
    to make an answer wrong."""
    return bool(gen.logprobs) and min(gen.logprobs) >= cutoff


def answer_key(out: dict) -> str:
    if out is None:
        return "invalid"
    if out["abstain"]:
        return "abstain"
    return " ".join(out["answer"].lower().split())


def self_consistency(samples: list[tuple[Generation, dict | None]], min_agree: int) -> tuple[int | None, float]:
    """Majority vote on the facts each sample states. Returns (index of a sample with the winning answer,
    agreement share) or (None, share) when too few agree."""
    keys = [answer_key(o) for _, o in samples]
    top, n = Counter(keys).most_common(1)[0]
    if n < min_agree or top in ("invalid",):
        return None, n / len(keys)
    return keys.index(top), n / len(keys)


def enforce_citations(out: dict, chunks: list[dict], store: PolicyStore, on: date) -> list[str]:
    """Problems with the citations (empty list = pass). The answer must cite at least one chunk; every cited
    id must exist in the store, be in force on ``on``, be one of the chunks retrieved for this question, and
    together the cited chunks must contain every number the answer states."""
    if out["abstain"]:
        return []
    problems = []
    if not out["citations"]:
        return ["no_citation"]
    known = {chunk_id(v): v for v in store.versions}
    retrieved = {c["id"] for c in chunks}
    cited_text = []
    for cid in out["citations"]:
        v = known.get(cid)
        if v is None:
            problems.append(f"unknown_id:{cid}")
        elif not v.in_force(on):
            problems.append(f"superseded:{cid}")
        elif cid not in retrieved:
            problems.append(f"not_retrieved:{cid}")
        else:
            cited_text.append(v.text)
    nums = set(re.findall(r"(?<![\w.-])\d+(?![\w-])", out["answer"]))
    have = set(re.findall(r"\d+", " ".join(cited_text)))
    if nums - have:
        problems.append("unsupported:" + ",".join(sorted(nums - have)))
    return problems


# --------------------------------------------------------------------------- pipeline + scoring
@dataclass
class Config:
    name: str
    dated: bool = False           # talk 18: retrieve only versions in force
    verifier: bool = False        # talk 18: grounding + freshness + PII verifier -> review queue
    idk_schema: bool = False      # control 5
    min_score: float = 0.0        # control 3 (0 = off)
    citations: bool = False       # control 4
    logprob_cutoff: float | None = None   # control 1
    sc_n: int = 1                 # control 2: samples per question
    sc_min_agree: int = 3
    sc_selective: bool = False    # sample N only when the first answer is in the log-prob grey band
    grey_band: tuple = (-1.0, -0.2)


@dataclass
class Outcome:
    action: str                   # "send", "abstain" (customer told "I don't know" + handoff), "review"
    answer: str | None
    calls: int
    reasons: list = field(default_factory=list)


def run_one(question: str, cfg: Config, lm: FakeLM, store: PolicyStore, on: date = TODAY,
            queue: ReviewQueue | None = None) -> Outcome:
    calls0 = FakeLM.calls
    chunks = retrieve(question, store, on, dated=cfg.dated, min_score=cfg.min_score)

    def done(action, answer=None, reasons=()):
        if action == "review" and queue is not None and answer:
            queue.put(question, answer, type("V", (), {"reasons": list(reasons)})())
        return Outcome(action, answer, FakeLM.calls - calls0, list(reasons))

    if cfg.min_score and not chunks:
        return done("abstain", None, ["below_min_score"])
    schema = cfg.idk_schema
    first = lm.generate(question, chunks, cfg.idk_schema, schema, 0)
    samples = [(first, parse(first, schema))]
    n = cfg.sc_n
    if cfg.sc_selective and cfg.logprob_cutoff is not None:
        lo, hi = cfg.grey_band
        n = cfg.sc_n if lo <= min(first.logprobs or [0]) < hi else 1
    for i in range(1, n):
        g = lm.generate(question, chunks, cfg.idk_schema, schema, i)
        samples.append((g, parse(g, schema)))
    if n > 1:
        idx, _share = self_consistency(samples, cfg.sc_min_agree)
        if idx is None:
            return done("abstain", None, ["samples_disagree"])
        gen, out = samples[idx]
    else:
        gen, out = samples[0]
    if out is None:
        return done("review", gen.raw, ["invalid_output"])
    if out["abstain"]:
        return done("abstain", None, ["model_abstained"])
    if cfg.logprob_cutoff is not None and n == 1 and not logprob_gate(gen, cfg.logprob_cutoff):
        return done("review", out["answer"], ["low_logprob"])
    if cfg.citations:
        probs = enforce_citations(out, chunks, store, on)
        if probs:
            return done("review", out["answer"], probs)
    if cfg.verifier:
        v = verify(out["answer"], [c["text"] for c in chunks], store, on)
        if v.route == "review":
            return done("review", out["answer"], v.reasons)
    return done("send", out["answer"])


def is_correct(item: dict, answer: str | None, store: PolicyStore, on: date = TODAY) -> bool:
    """Ground truth for a SENT answer. Unanswerable: any sent answer is wrong (the right move was to abstain).
    Answerable / stale: the answer must state the current value of the rule and no number outside the policy."""
    if item["kind"] == "unanswerable":
        return False
    vals = claimed_values(answer or "")
    cur = store.current_value(item["rule"], on)
    if vals.get(item["rule"]) != {cur}:
        return False
    if any(c in answer for c in INVENTED_CLAIMS):
        return False
    return "%" not in answer and not re.search(r"within (14|21|45|90) days", answer)


def evaluate(cfg: Config, seeds: int = 20, store: PolicyStore | None = None) -> dict:
    store = store or PolicyStore()
    tot = Counter()
    by_kind = {k: Counter() for k in ("answerable", "unanswerable", "stale")}
    calls = 0
    for seed in range(seeds):
        lm = FakeLM(seed)
        for item in QUESTIONS:
            o = run_one(item["q"], cfg, lm, store)
            calls += o.calls
            bk = by_kind[item["kind"]]
            bk["n"] += 1
            tot["n"] += 1
            if o.action == "send":
                ok = is_correct(item, o.answer, store)
                bk["sent_ok" if ok else "sent_wrong"] += 1
                tot["sent_ok" if ok else "sent_wrong"] += 1
            else:
                bk[o.action] += 1
                tot[o.action] += 1
    ans = by_kind["answerable"]
    held = tot["abstain"] + tot["review"]
    return {
        "config": cfg.name, "trials": tot["n"],
        "wrong_to_customer": round(tot["sent_wrong"] / tot["n"], 4),
        "wrong_to_customer_count": tot["sent_wrong"],
        "held_back_rate": round(held / tot["n"], 4),
        "abstain_rate": round(tot["abstain"] / tot["n"], 4),
        "review_rate": round(tot["review"] / tot["n"], 4),
        "false_abstain_rate": round((ans["abstain"] + ans["review"]) / ans["n"], 4),
        "correct_sent_rate": round(tot["sent_ok"] / tot["n"], 4),
        "cost_multiplier": round(calls / tot["n"], 3),
        "by_kind": {k: dict(v) for k, v in by_kind.items()},
    }


BASE = dict(dated=True, verifier=True)
LADDER = [
    Config("0 incident: undated index, plain prompt, no checks"),
    Config("1 talk 18: dated retrieval + verifier + review queue", **BASE),
    Config("2 + 'I don't know' few-shot prompt + JSON schema", **BASE, idk_schema=True),
    Config("3 + retrieval score >= 0.5, else abstain", **BASE, idk_schema=True, min_score=0.5),
    Config("4 + citation enforcement", **BASE, idk_schema=True, min_score=0.5, citations=True),
    Config("5 + log-prob gate (min token >= -1.0)", **BASE, idk_schema=True, min_score=0.5, citations=True,
           logprob_cutoff=-1.0),
    Config("6 + self-consistency N=5 on every question", **BASE, idk_schema=True, min_score=0.5,
           citations=True, sc_n=5),
    Config("7 + self-consistency N=5 only in the log-prob grey band", **BASE, idk_schema=True, min_score=0.5,
           citations=True, logprob_cutoff=-1.0, sc_n=5, sc_selective=True),
]


def results(seeds: int = 20) -> dict:
    ladder = [evaluate(c, seeds) for c in LADDER]
    # sweeps isolate one control on the dated index with the few-shot prompt, no verifier and no other gate
    iso = dict(dated=True, idk_schema=True)
    score_sweep = []
    for t in (0.0, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9):
        r = evaluate(Config(f"score>={t}", **iso, min_score=t), seeds)
        score_sweep.append({"min_score": t, **{k: r[k] for k in ("wrong_to_customer", "held_back_rate",
                                                                    "false_abstain_rate")}})
    lp_sweep = []
    for c in (None, -0.1, -0.25, -0.5, -1.0, -1.5, -2.0, -3.0):
        r = evaluate(Config(f"logprob>={c}", **iso, logprob_cutoff=c), seeds)
        lp_sweep.append({"cutoff": c, **{k: r[k] for k in ("wrong_to_customer", "held_back_rate",
                                                           "false_abstain_rate")}})
    sc_sweep = []
    for n, agree in ((1, 1), (3, 2), (5, 3), (5, 4), (7, 4)):
        r = evaluate(Config(f"sc n={n}", **iso, sc_n=n, sc_min_agree=agree), seeds)
        sc_sweep.append({"n": n, "min_agree": agree, **{k: r[k] for k in (
            "wrong_to_customer", "held_back_rate", "false_abstain_rate", "cost_multiplier")}})
    return {"note": ("simulation: seeded fake model with invented failure rates, fictional store; every number "
                     "is an exact count on the labelled set, not a production measurement"),
            "question_set": dict(Counter(q["kind"] for q in QUESTIONS)), "seeds": seeds,
            "ladder": ladder, "min_score_sweep": score_sweep, "logprob_sweep": lp_sweep,
            "self_consistency_sweep": sc_sweep,
            "isolated_controls": {k: evaluate(Config(k, **iso, **kw), seeds) for k, kw in (
                ("none", {}), ("min_score_0.5", {"min_score": 0.5}), ("citations", {"citations": True}),
                ("logprob_-1.0", {"logprob_cutoff": -1.0}), ("self_consistency_5", {"sc_n": 5}),
                ("talk18_verifier", {"verifier": True}))}}


def safe_log_line(question: str, o: Outcome) -> str:
    """One log line per decision, redacted (no raw contact details reach logs)."""
    return f"action={o.action} calls={o.calls} reasons={','.join(o.reasons) or '-'} q={redact(question)!r}"

