"""The single support agent: an explicit state machine over a deterministic pipeline.

Nodes (each returns the next node):
  screen_input -> classify -> {reply_rules | order_lookup | answer_knowledge | refund_extract |
  cancel_confirm | handoff | ask_missing}; refund_extract -> refund_propose -> {await_confirmation |
  alternatives}; every answer -> ground -> screen_output -> done.
Branches: missing order id -> ask; refund not eligible -> alternatives; cancellation -> confirmation
path that files a human ticket; extraction invalid -> retry -> other model -> ask the user.
The model drafts text and extracts fields. Code owns routing, policy, approvals and grounding.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import guardrails
from .approvals import ApprovalGate
from .cache import SemanticCache
from .context import compact
from .grounding import check as grounding_check
from .knowledge import KnowledgeRouter
from .llm import count_tokens
from .memory import FactMemory
from .observability import Metrics, Step, Trace, TraceStore, eval_tool_call
from .pii import contains_pii, pseudonymise, redact
from .rag import KnowledgeBase
from .resilience import Degraded, ModelGateway
from .router import HITL_INTENTS, Route, route
from .structured import extract
from .tools import ORDERS, Registry

NO_ANSWER = ("I couldn't find that in our policies, so I won't guess. I can connect you with a team "
             "member if you'd like.")
DEGRADED = ("Our assistant is having trouble reaching its language service right now. Your question is "
            "saved; please try again in a minute or ask for a team member.")
MIN_RETRIEVAL_SCORE = 0.20   # below this, retrieval is "nothing relevant", not a weak answer


@dataclass
class Turn:
    text: str
    route: Route | None = None
    answer: str = ""
    evidence: list = field(default_factory=list)
    retrieved: list = field(default_factory=list)
    quarantined: list = field(default_factory=list)
    proposal: dict | None = None
    model_calls: int = 0
    flags: list = field(default_factory=list)
    order_id: str | None = None
    cache_candidate: bool = False


class Agent:
    def __init__(self, gateway: ModelGateway, kb: KnowledgeBase, cache: SemanticCache, registry: Registry,
                 gate: ApprovalGate, facts: FactMemory, metrics: Metrics | None = None,
                 traces: TraceStore | None = None, router_enabled: bool = True, grounding: bool = True) -> None:
        self.gw, self.kb, self.cache, self.reg, self.gate, self.facts = gateway, kb, cache, registry, gate, facts
        self.metrics = metrics or Metrics()
        self.traces = traces or TraceStore()
        self.router_enabled = router_enabled
        self.grounding = grounding
        self.knowledge = KnowledgeRouter(kb, ORDERS)
        self.conversations: dict[str, list[str]] = {}

    # ------------------------------------------------------------------ entry point
    async def handle(self, text: str, identity: dict, conversation_id: str = "c-1") -> dict:
        t0 = time.perf_counter()
        trace = Trace(conversation_id, f"wf-{conversation_id}", pseudonymise(identity["sub"]))
        turn = Turn(text)
        node = "screen_input"
        visited = []
        while node != "done":
            visited.append(node)
            if len(visited) > 20:
                raise RuntimeError("orchestration loop")  # graph has no cycles; guard anyway
            node = await getattr(self, "n_" + node)(turn, identity, trace)
        hist = self.conversations.setdefault(conversation_id, [])
        hist.append(redact(text))
        trace.eval_scores = {"path": visited}
        self.traces.save(trace)
        intent = turn.route.intent if turn.route else "blocked"
        self.metrics.latency.labels("/v1/chat").observe(time.perf_counter() - t0)
        return {"answer": turn.answer, "intent": intent, "tier": turn.route.tier if turn.route else "none",
                "proposal": turn.proposal, "run_id": trace.run_id, "cost_usd": trace.cost_usd,
                "tokens": sum(trace.tokens), "model_calls": turn.model_calls, "path": visited,
                "flags": turn.flags, "retrieved": [c["id"] for c in turn.retrieved], "trace": trace}

    # ------------------------------------------------------------------ nodes
    async def n_screen_input(self, turn, ident, trace):
        g = guardrails.check_input(turn.text)
        trace.add(Step("screen_input", detail={"blocked": g.blocked, "reasons": g.reasons}))
        if g.blocked:
            turn.answer = "I can't help with that request. I can help with orders, returns and store policies."
            turn.flags.append("injection_blocked")
            return "done"
        return "classify"

    async def n_classify(self, turn, ident, trace):
        r = route(turn.text, enabled=self.router_enabled)
        ref = self.facts.resolve_reference(ident["sub"], turn.text) if self.facts else None
        turn.order_id = r.order_id or ref
        if ref and r.intent in ("unknown", "complex", "faq_policy"):
            r = Route("order_status", "rules", 1.0, ref)
        turn.route = r
        trace.add(Step("classify", detail={"intent": r.intent, "tier": r.tier, "order_id": turn.order_id,
                                           "resolved_reference": bool(ref)}))
        if r.intent == "cancellation" and turn.order_id:
            return "cancel_confirm"
        if r.intent in HITL_INTENTS:
            return "handoff"
        if r.intent in ("greeting", "out_of_scope"):
            return "reply_rules"
        if r.intent == "order_status":
            return "order_lookup" if turn.order_id else "ask_missing"
        if r.intent == "refund_request":
            return "refund_extract" if turn.order_id else "ask_missing"
        return "answer_knowledge"

    async def n_reply_rules(self, turn, ident, trace):
        turn.answer = ("Hi! I can help with orders, returns, refunds and store policies."
                       if turn.route.intent == "greeting" else
                       "That's outside what I can help with here. I can help with orders, returns and policies.")
        return "screen_output"

    async def n_ask_missing(self, turn, ident, trace):
        turn.answer = "Could you share your order number? It looks like QM- followed by five digits."
        trace.add(Step("request_missing_info", detail={"missing": ["order_id"]}))
        return "screen_output"

    async def n_order_lookup(self, turn, ident, trace):
        res = await self.reg.call("get_order_status", {"order_id": turn.order_id}, ident)
        self._tool_step(trace, "get_order_status", {"order_id": turn.order_id}, res, ident)
        if not res.ok:
            if res.error["code"] in ("timeout", "upstream"):
                turn.answer = "I can't reach the order system right now. Please try again shortly."
                turn.flags.append("tool_degraded")
            else:
                turn.answer = "I couldn't find that order on your account."
            return "screen_output"
        o = res.output
        turn.evidence.append(o)
        eta = f" It should arrive in {o['eta_days']} days." if o.get("eta_days") else ""
        turn.answer = f"Order {o['order_id']} is {o['status'].replace('_', ' ')}.{eta}"
        return "ground"

    async def n_answer_knowledge(self, turn, ident, trace):
        scopes = ident.get("scope", "")
        tenant = ident.get("tenant", "")
        hit = self.cache.get(tenant, scopes, turn.route.intent, turn.text)
        self.metrics.cache.labels("hit" if hit else "miss").inc()
        if hit:
            trace.add(Step("cache_hit", detail={"chunks": hit.chunk_ids}))
            turn.answer, turn.retrieved = hit.answer, [{"id": c} for c in hit.chunk_ids]
            return "screen_output"
        k = self.knowledge.answer(turn.text, ident["sub"])
        if k["source"] in ("graph", "sql"):
            trace.add(Step("structured_lookup", detail={"source": k["source"]}))
            turn.evidence.append(k)
            turn.answer = ("Yes, those are compatible." if k["answer"] == "yes" else "No, those aren't compatible."
                           ) if k["source"] == "graph" and k["facts"][1] == "fits" else (
                "Yes, that policy covers it." if k["source"] == "graph" and k["answer"] == "yes" else (
                    "No, that policy doesn't cover it." if k["source"] == "graph" else
                    f"The answer is {k['answer']}."))
            return "ground"
        res = await self.reg.call("search_policy", {"query": turn.text[:300]}, ident)
        self._tool_step(trace, "search_policy", {"query": turn.text[:300]}, res, ident)
        chunks = res.output["chunks"] if res.ok else []
        relevant = [c for c in chunks if c["score"] >= MIN_RETRIEVAL_SCORE]
        safe, quarantined = guardrails.isolate(relevant)
        turn.quarantined = quarantined
        if quarantined:
            turn.flags.append("indirect_injection_quarantined")
        turn.retrieved = safe
        trace.add(Step("retrieve", detail={"ids": [c["id"] for c in safe], "quarantined": quarantined,
                                           "top_score": chunks[0]["score"] if chunks else 0.0}))
        if not safe:
            turn.answer = NO_ANSWER
            turn.flags.append("no_relevant_knowledge")
            return "screen_output"
        tier = "large" if turn.route.tier == "large" else "small"
        sensitive = contains_pii(turn.text)
        prompt = ("Answer the customer using only the policy data.\nCONTEXT: "
                  + " ".join(c["text"] for c in safe)
                  + "\nQUESTION: " + redact(turn.text))
        stats: dict = {}
        try:
            comp = await self.gw.complete("small" if sensitive else tier, prompt, 120, stats,
                                          allow_third_party=not sensitive)
        except Degraded as e:
            trace.add(Step("draft_answer", error=str(e)))
            turn.answer = DEGRADED
            turn.flags.append("degraded")
            return "screen_output"
        turn.model_calls += 1
        self._model_step(trace, "draft_answer", comp, stats)
        turn.evidence.extend(c["text"] for c in safe)
        turn.answer = comp.text.split(": ", 1)[-1] if comp.text.startswith("[") else comp.text
        turn.cache_candidate = not sensitive
        return "ground"

    async def n_refund_extract(self, turn, ident, trace):
        o = ORDERS.get(turn.order_id or "")
        # data minimisation: the extractor sees the message only, never the order record or history
        draft, info = await extract(self.gw.providers, "Extract refund fields.\nMESSAGE: " + redact(turn.text))
        turn.model_calls += len(info["attempts"])
        for tier, u in info["usage"]:
            trace.add(Step("extract_action_fields", model=self.gw.providers[tier].name,
                           model_version=self.gw.providers[tier].model_version, tokens_in=u.tokens_in,
                           tokens_out=u.tokens_out, cost_usd=u.cost_usd, retries=info["invalid"]))
        if draft is None:
            turn.answer = "I couldn't read the refund details. Which order is it, and what went wrong?"
            turn.flags.append("extraction_failed")
            return "screen_output"
        reason = draft.reason if draft.reason in ("damaged", "not_as_described", "changed_mind") else "other"
        # amount comes from code (order total), never from the model or the user's claim
        amount = o["total"] if o and o["owner"] == ident["sub"] else 0.01
        args = {"order_id": turn.order_id, "amount": amount, "reason": reason}
        prop = self.gate.propose(trace.workflow_id, "issue_refund", args, ident)
        trace.add(Step("approve_high_risk", tool="issue_refund", args=args, detail={"verdict": prop["status"],
                                                                                    "reason": prop.get("reason")}))
        turn.proposal = prop
        if prop["status"] == "rejected":
            return "alternatives"
        if prop["status"] == "pending_human":
            turn.answer = ("This refund needs a quick review by our team; I've sent it to them with your details. "
                           "You'll hear back by email.")
            return "screen_output"
        turn.answer = f"Please confirm: {prop['summary']} Reply with your confirmation to proceed."
        return "screen_output"

    async def n_alternatives(self, turn, ident, trace):
        reason = turn.proposal.get("reason", "")
        msgs = {"outside_window": "This order is outside the return window, so I can't refund it.",
                "final_sale": "Final-sale items can't be refunded.",
                "not_owner": "I couldn't find that order on your account.",
                "already_refunded": "That order has already been refunded.",
                "not_delivered": "That order hasn't been delivered yet, so it can't be refunded."}
        turn.answer = msgs.get(reason, "I can't process that refund.") + (
            " I can open a ticket for a team member to review an exception, or help with a replacement.")
        return "screen_output"

    async def n_cancel_confirm(self, turn, ident, trace):
        prop = self.gate.propose(trace.workflow_id, "request_cancellation", {"order_id": turn.order_id}, ident)
        turn.proposal = prop
        trace.add(Step("approve_high_risk", tool="request_cancellation", args={"order_id": turn.order_id},
                       detail={"verdict": prop["status"], "reason": prop.get("reason")}))
        if prop["status"] == "rejected":
            return "handoff"
        turn.answer = f"Please confirm: {prop['summary']} A team member completes cancellations."
        return "screen_output"

    async def n_handoff(self, turn, ident, trace):
        topic = turn.route.intent if turn.route.intent in ("cancellation", "billing_dispute") else "other"
        args = {"topic": topic, "summary": redact(turn.text)[:500]}
        res = await self.reg.call("escalate_to_human", args, ident)
        self._tool_step(trace, "escalate_to_human", args, res, ident)
        turn.answer = ("I've passed this to a team member who handles these requests"
                       + (f" (ticket {res.output['ticket_id']})." if res.ok else ". Please hold on."))
        turn.flags.append("escalated")
        return "screen_output"

    async def n_ground(self, turn, ident, trace):
        if not self.grounding:
            return "screen_output"
        g = grounding_check(turn.answer, turn.evidence)
        trace.add(Step("grounding", detail=g))
        if not g["grounded"]:
            turn.flags.append("ungrounded_blocked")
            turn.answer = ("I'm not able to confirm those details right now, so I won't guess. "
                           "A team member can check for you.")
        return "screen_output"

    async def n_screen_output(self, turn, ident, trace):
        out = guardrails.check_output(turn.answer)
        if out.violations:
            turn.flags.extend(out.violations)
        turn.answer = out.text
        trace.add(Step("screen_output", detail={"violations": out.violations}))
        if turn.cache_candidate and not turn.flags:
            self.cache.put(ident.get("tenant", ""), ident.get("scope", ""), turn.route.intent, turn.text,
                           turn.answer, [c["id"] for c in turn.retrieved])
        return "done"

    # ------------------------------------------------------------------ helpers
    def _tool_step(self, trace, name, args, res, ident):
        checks = eval_tool_call(name, args, res.ok, ident, ORDERS)
        for c, ok in checks.items():
            self.metrics.tool_evals.labels(c, "pass" if ok else "fail").inc()
        trace.add(Step("tool_call", tool=name, args=args, latency_s=res.latency_s, retries=max(0, res.attempts - 1),
                       error=(res.error or {}).get("code"), detail={"online_checks": checks}))

    def _model_step(self, trace, name, comp, stats):
        tier = "large" if comp.model.startswith(("large", "fallback")) else "small"
        self.metrics.tokens.labels(tier, "in").inc(comp.usage.tokens_in)
        self.metrics.tokens.labels(tier, "out").inc(comp.usage.tokens_out)
        self.metrics.cost.labels(tier).inc(comp.usage.cost_usd)
        trace.add(Step(name, model=comp.model, model_version=comp.model_version, tokens_in=comp.usage.tokens_in,
                       tokens_out=comp.usage.tokens_out, cost_usd=comp.usage.cost_usd,
                       retries=stats.get("retries", 0), fallback=stats.get("fallback")))

    def context_for_next_turn(self, conversation_id: str) -> dict:
        """Context delta: summary + last turns, not the full history."""
        return compact(self.conversations.get(conversation_id, []))

    def estimate_tokens(self, text: str) -> int:
        return count_tokens(text) + 250
