"""The core AI service logic: one request through guardrails, router, cache, admission and model gateway."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from .admission import Admission
from .auth import Identity
from .cache import SemanticCache
from .guardrails import check_input, check_output
from .knowledge import KnowledgeIndex
from .observability import Metrics, functional_eval
from .pii import redact
from .providers import count_tokens
from .resilience import ModelGateway, Unavailable
from .router import route
from .tools import Commerce, NotFound

DEGRADED = ("I can't answer that reliably right now. Your question is saved and a support specialist "
            "will follow up; nothing about your order has been changed.")
HANDOFF = "I'm connecting you with a support specialist who can help with this directly."
MAX_OUTPUT = {"small": 200, "large": 400}


@dataclass
class Result:
    status: int
    answer: str
    intent: str
    rung: str
    cached: bool = False
    degraded: bool = False
    cost_usd: float = 0.0
    tokens: int = 0
    tool_calls: list = field(default_factory=list)
    eval_pass: bool = True
    reason: str = "ok"
    retry_after_s: int = 0
    model: str | None = None
    sources: list = field(default_factory=list)


class SupportAgent:
    def __init__(self, admission: Admission, cache: SemanticCache, gateway: ModelGateway,
                 index: KnowledgeIndex | None = None, commerce: Commerce | None = None,
                 metrics: Metrics | None = None, tracer=None, clock=None,
                 cache_enabled: bool = True, ladder_enabled: bool = True, guards_enabled: bool = True,
                 rate_check: bool = True) -> None:
        self.admission = admission
        self.cache = cache
        self.gateway = gateway
        self.index = index or KnowledgeIndex()
        self.commerce = commerce or Commerce()
        self.metrics = metrics or Metrics()
        self.tracer = tracer
        self.clock = clock
        self.cache_enabled = cache_enabled
        self.ladder_enabled = ladder_enabled
        self.guards_enabled = guards_enabled
        self.rate_check = rate_check  # False when an upstream gateway already enforced it
        self.handoffs: list[dict] = []

    def _now(self) -> float:
        return self.clock.now() if self.clock else time.time()

    def handle(self, ident: Identity, text: str) -> Result:
        start = self._now()
        if self.tracer is not None:
            with self.tracer.start_as_current_span("agent.handle") as span:
                res = self._handle(ident, text)
                span.set_attribute("user.message", text)  # redacted by the exporter
                span.set_attribute("intent", res.intent)
                span.set_attribute("rung", res.rung)
                span.set_attribute("answer", res.answer)
                span.set_attribute("cost_usd", res.cost_usd)
        else:
            res = self._handle(ident, text)
        self.metrics.requests.labels(res.intent, res.reason).inc()
        self.metrics.latency.labels(res.intent).observe(max(0.0, self._now() - start))
        return res

    def _handle(self, ident: Identity, text: str) -> Result:
        # rate gate first: every request costs a token, including blocked and rule-answered ones,
        # so an attacker cannot spam injection attempts or greetings for free
        d = self.admission.check_rate(ident.tenant, ident.user_id, ident.tier) if self.rate_check else None
        if d is not None and not d.allowed:
            self.metrics.rejections.labels(d.reason).inc()
            return Result(429, "Too many requests, please retry shortly.", "rate_limited", "gateway",
                          reason=d.reason, retry_after_s=d.retry_after_s)
        if self.guards_enabled:
            g = check_input(text)
            if not g.allowed:
                return Result(400, "I can't help with that request.", "blocked", "guardrail", reason=g.reason)
        r = route(text, ladder_enabled=self.ladder_enabled)
        if r.intent == "escalate":
            self.handoffs.append({"user": ident.user_id, "text": redact(text)})
            return Result(200, HANDOFF, "escalate", "human", reason="handoff")
        if r.model_tier is None:
            return self._no_llm(ident, r, text)
        if self.cache_enabled:
            hit = self.cache.get(ident.tenant, ident.scopes, r.intent, text)
            if hit:
                self.metrics.cache.labels("hit").inc()
                return Result(200, hit["answer"], r.intent, "cache", cached=True, sources=hit["sources"])
            self.metrics.cache.labels("miss").inc()
        return self._llm(ident, r, text)

    def _no_llm(self, ident: Identity, r, text: str) -> Result:
        calls = []
        if r.intent == "greeting":
            ans = "Hi! I can help with orders, returns, refunds and store policies."
        elif r.intent == "thanks":
            ans = "You're welcome. Anything else I can help with?"
        elif r.intent == "order_status":
            try:
                s = self.commerce.order_status(r.order_id, ident.user_id)
                calls.append("order_status")
                ans = f"Order {s['order_id']} is {s['status']}; estimated delivery {s['eta']}."
            except NotFound:
                calls.append("order_status")
                ans = "I couldn't find that order on your account."
        elif r.intent == "price":
            m = re.search(r"\bSKU-[A-Z]+\b", text.upper())
            if m and m.group(0) in self.commerce.prices:
                ans = f"{m.group(0)} is currently ${self.commerce.price(m.group(0)):.2f}."
                calls.append("price")
            else:
                ans = "Please share the product SKU and I'll look up the live price."
        else:  # pragma: no cover - router never sends other intents here
            ans = HANDOFF
        ev = functional_eval(r.intent, calls) if r.intent in ("order_status", "price") and calls else {"pass": True}
        return Result(200, ans, r.intent, r.rung, tool_calls=calls, eval_pass=ev["pass"])

    def _llm(self, ident: Identity, r, text: str) -> Result:
        tier = r.model_tier
        sources, context = [], ""
        if r.intent in ("policy", "unknown", "complex") or tier == "large":
            hits = self.index.search(text)
            sources = [c.chunk_id for c, _ in hits]
            context = " ".join(c.text for c, _ in hits)
        prompt = f"policy: {context}\nuser: {redact(text)}"
        estimate = count_tokens(prompt) + MAX_OUTPUT[tier]
        d = self.admission.reserve_tokens(ident.tenant, ident.user_id, ident.tier, estimate)
        if not d.allowed:
            self.metrics.rejections.labels(d.reason).inc()
            return Result(429, "You've reached your usage limit for now.", r.intent, r.rung,
                          reason=d.reason, retry_after_s=d.retry_after_s)
        slot = self.admission.acquire_model_slot()
        if slot is None:
            self.admission.settle_tokens(ident.tenant, ident.user_id, estimate, 0)
            self.metrics.rejections.labels("model_concurrency").inc()
            return Result(429, "We're busy, please retry shortly.", r.intent, r.rung,
                          reason="model_concurrency", retry_after_s=1)
        try:
            if r.intent == "refund":
                answer = ("I can start a refund review for that order. Use the refund form so it runs as a "
                          "tracked job; amounts are checked against the order and policy.")
            elif sources:
                answer = context.split(". ")[0].rstrip(".") + "."
            else:
                answer = "I don't have a policy that covers that; a specialist can help."
            c = self.gateway.complete(tier, prompt, max_output_tokens=MAX_OUTPUT[tier], answer=answer)
        except Unavailable:
            self.admission.settle_tokens(ident.tenant, ident.user_id, estimate, 0)
            return Result(503, DEGRADED, r.intent, r.rung, degraded=True, reason="degraded")
        finally:
            self.admission.release_model_slot(slot)
        used = c.input_tokens + c.output_tokens
        self.admission.settle_tokens(ident.tenant, ident.user_id, estimate, used)
        self.metrics.tokens.labels(c.model, "input").inc(c.input_tokens)
        self.metrics.tokens.labels(c.model, "output").inc(c.output_tokens)
        self.metrics.cost.labels(c.model).inc(c.cost_usd)
        out = check_output(c.text, r.intent) if self.guards_enabled else None
        if out is not None and not out.allowed:
            return Result(200, HANDOFF, r.intent, r.rung, cost_usd=c.cost_usd, tokens=used,
                          reason=out.reason, model=c.model)
        final = out.text if out else c.text
        if self.cache_enabled and sources:
            self.cache.put(ident.tenant, ident.scopes, r.intent, text, final, sources)
        ev = functional_eval(r.intent, []) if r.intent in ("policy", "refund") else {"pass": True}
        return Result(200, final, r.intent, r.rung, cost_usd=c.cost_usd, tokens=used, model=c.model,
                      sources=sources, eval_pass=ev["pass"])

    def on_policy_change(self, doc_id: str, paragraphs: list[str]) -> dict:
        """CDC event: re-index only changed chunks, invalidate only cache entries built on them."""
        changed = self.index.apply_change(doc_id, paragraphs)
        removed = self.cache.invalidate_chunks(changed)
        return {"changed_chunks": sorted(changed), "cache_entries_invalidated": removed}
