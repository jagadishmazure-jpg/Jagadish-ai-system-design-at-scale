"""Metrics, per-run traces, SLO alerts and online functional evals of tool calls.

- Prometheus metrics on a private registry (exposed at /metrics).
- One Trace per run with the full anatomy: model name/version, prompt version, workflow and
  conversation ids, per-step tool name + REDACTED args, latency, TTFT, tokens in/out, cost,
  retries, fallbacks, errors and eval scores. Each step is mirrored to an OpenTelemetry span
  (in-memory exporter by default; point OTEL_EXPORTER_* at a collector in compose).
- Retention: traces older than TRACE_RETENTION_DAYS are pruned by TraceStore.prune().
"""
from __future__ import annotations

import os
import time
import uuid
from dataclasses import asdict, dataclass, field

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest

from .pii import redact, redact_obj

PROMPT_VERSION = "support-v3"
RETENTION_DAYS = int(os.environ.get("TRACE_RETENTION_DAYS", "30"))

_exporter = InMemorySpanExporter()
_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(_exporter))
tracer = _provider.get_tracer("sysdesign")


def otel_spans():
    return _exporter.get_finished_spans()


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        r = self.registry
        self.requests = Counter("requests_total", "Requests by route and outcome", ["path", "status"], registry=r)
        self.latency = Histogram("request_latency_seconds", "End-to-end latency", ["path"], registry=r,
                                 buckets=(.01, .025, .05, .1, .25, .5, 1, 2.5, 5))
        self.ttft = Histogram("time_to_first_token_seconds", "Streaming TTFT", registry=r,
                              buckets=(.005, .01, .025, .05, .1, .25, .5, 1))
        self.tokens = Counter("llm_tokens_total", "LLM tokens", ["tier", "direction"], registry=r)
        self.cost = Counter("llm_cost_usd_total", "LLM cost in USD", ["tier"], registry=r)
        self.cache = Counter("semantic_cache_total", "Cache lookups", ["result"], registry=r)
        self.rejections = Counter("admission_rejections_total", "429s", ["reason"], registry=r)
        self.tool_evals = Counter("tool_call_evals_total", "Online tool-call checks", ["check", "result"],
                                  registry=r)

    def expose(self) -> bytes:
        return generate_latest(self.registry)


@dataclass
class Step:
    name: str
    latency_s: float = 0.0
    tool: str | None = None
    args: dict | None = None
    model: str | None = None
    model_version: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    retries: int = 0
    fallback: str | None = None
    error: str | None = None
    detail: dict = field(default_factory=dict)


@dataclass
class Trace:
    conversation_id: str
    workflow_id: str
    user: str                     # pseudonymised
    prompt_version: str = PROMPT_VERSION
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    started: float = field(default_factory=time.time)
    steps: list[Step] = field(default_factory=list)
    ttft_s: float | None = None
    eval_scores: dict = field(default_factory=dict)
    feedback: str | None = None

    def add(self, step: Step) -> Step:
        step.args = redact_obj(step.args) if step.args else step.args
        step.error = redact(step.error) if step.error else step.error
        step.detail = redact_obj(step.detail)
        self.steps.append(step)
        with tracer.start_as_current_span(step.name) as sp:
            sp.set_attribute("run_id", self.run_id)
            sp.set_attribute("workflow_id", self.workflow_id)
            sp.set_attribute("prompt_version", self.prompt_version)
            for k in ("tool", "model", "model_version", "fallback", "error"):
                v = getattr(step, k)
                if v:
                    sp.set_attribute(k, str(v))
            if step.args:
                sp.set_attribute("tool_args", str(step.args))
            sp.set_attribute("tokens_in", step.tokens_in)
            sp.set_attribute("tokens_out", step.tokens_out)
            sp.set_attribute("cost_usd", step.cost_usd)
        return step

    @property
    def cost_usd(self) -> float:
        return round(sum(s.cost_usd for s in self.steps), 8)

    @property
    def tokens(self) -> tuple[int, int]:
        return sum(s.tokens_in for s in self.steps), sum(s.tokens_out for s in self.steps)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["cost_usd"] = self.cost_usd
        d["tokens_in"], d["tokens_out"] = self.tokens
        return d


class TraceStore:
    def __init__(self, retention_days: int = RETENTION_DAYS) -> None:
        self.retention_days = retention_days
        self.traces: dict[str, Trace] = {}

    def save(self, t: Trace) -> None:
        self.traces[t.run_id] = t

    def prune(self, now: float | None = None) -> int:
        cutoff = (now or time.time()) - self.retention_days * 86400
        old = [k for k, t in self.traces.items() if t.started < cutoff]
        for k in old:
            del self.traces[k]
        return len(old)


# ---------------- SLO alerts ----------------
@dataclass
class SLO:
    p95_latency_s: float = 3.0
    max_cost_per_hour_usd: float = 5.0
    max_tokens_per_minute: int = 200_000
    max_error_rate: float = 0.01


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    v = sorted(values)
    k = (len(v) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def evaluate_slos(latencies: list[float], errors: int, total: int, cost_usd: float, window_s: float,
                  tokens: int, slo: SLO | None = None) -> list[dict]:
    """Return firing alerts. A window with no traffic does not 'pass' the error SLO by dividing by 0."""
    slo = slo or SLO()
    alerts = []
    p95 = percentile(latencies, 95)
    if p95 > slo.p95_latency_s:
        alerts.append({"alert": "p95_latency", "value": round(p95, 4), "threshold": slo.p95_latency_s})
    if total and errors / total > slo.max_error_rate:
        alerts.append({"alert": "error_rate", "value": round(errors / total, 4), "threshold": slo.max_error_rate})
    per_hour = cost_usd / window_s * 3600 if window_s > 0 else 0.0
    if per_hour > slo.max_cost_per_hour_usd:
        alerts.append({"alert": "cost_rate", "value": round(per_hour, 4), "threshold": slo.max_cost_per_hour_usd})
    per_min = tokens / window_s * 60 if window_s > 0 else 0.0
    if per_min > slo.max_tokens_per_minute:
        alerts.append({"alert": "token_rate", "value": round(per_min), "threshold": slo.max_tokens_per_minute})
    return alerts


# ---------------- online functional evals of tool calls ----------------
def eval_tool_call(tool: str, args: dict, result_ok: bool, identity: dict, orders: dict) -> dict:
    """Cheap checks run on every live tool call (not an LLM judge)."""
    checks = {}
    if "order_id" in args:
        o = orders.get(args["order_id"])
        checks["order_belongs_to_caller"] = bool(o and o["owner"] == identity.get("sub")) or not result_ok
    if tool == "issue_refund":
        o = orders.get(args.get("order_id"), {})
        checks["refund_within_total"] = float(args.get("amount", 0)) <= o.get("total", 0)
    checks["schema_valid"] = isinstance(args, dict)
    return checks
