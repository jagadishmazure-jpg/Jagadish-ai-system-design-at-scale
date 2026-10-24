"""Prometheus metrics, OpenTelemetry tracing with PII redaction at export, SLO alerts,
and online functional evals of tool calls."""
from __future__ import annotations

import math
from collections.abc import Sequence

from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from prometheus_client import CollectorRegistry, Counter, Histogram

from .pii import redact_obj


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.requests = Counter("sd_requests_total", "Requests by route and outcome", ["intent", "outcome"],
                                registry=self.registry)
        self.latency = Histogram("sd_request_latency_seconds", "End-to-end latency", ["intent"],
                                 buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 10), registry=self.registry)
        self.ttft = Histogram("sd_time_to_first_token_seconds", "Streaming time to first token",
                              buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2), registry=self.registry)
        self.tokens = Counter("sd_llm_tokens_total", "LLM tokens", ["model", "kind"], registry=self.registry)
        self.cost = Counter("sd_llm_cost_usd_total", "LLM spend (USD)", ["model"], registry=self.registry)
        self.cache = Counter("sd_cache_events_total", "Semantic cache events", ["event"], registry=self.registry)
        self.http = Counter("sd_http_requests_total", "Every HTTP response by route and status (error-rate SLO)",
                            ["route", "status"], registry=self.registry)
        self.rejections = Counter("sd_admission_rejections_total", "429s by reason", ["reason"],
                                  registry=self.registry)


class InMemoryRedactingExporter(SpanExporter):
    """Keeps exported spans in memory with every attribute redacted (swap for OTLP in compose)."""

    def __init__(self) -> None:
        self.spans: list[dict] = []

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        for s in spans:
            self.spans.append({"name": s.name, "attributes": redact_obj(dict(s.attributes or {})),
                               "trace_id": format(s.context.trace_id, "032x")})
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:  # pragma: no cover
        pass


def make_tracer():
    exporter = InMemoryRedactingExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("sysdesign"), exporter


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile; empty input returns NaN rather than a flattering 0."""
    if not values:
        return math.nan
    v = sorted(values)
    k = max(0, min(len(v) - 1, math.ceil(p / 100 * len(v)) - 1))
    return v[k]


def slo_alerts(latencies: list[float], cost_usd: float, tokens: int, window_h: float,
               p95_target_s: float = 3.0, cost_per_h_budget: float = 5.0, tokens_per_h_budget: int = 2_000_000,
               min_samples: int = 20) -> list[dict]:
    if window_h <= 0:
        raise ValueError("window must be positive")
    alerts = []
    if len(latencies) >= min_samples:
        p95 = percentile(latencies, 95)
        if p95 > p95_target_s:
            alerts.append({"slo": "p95_latency", "value": round(p95, 3), "target": p95_target_s})
    elif latencies:
        alerts.append({"slo": "insufficient_samples", "value": len(latencies), "target": min_samples})
    if cost_usd / window_h > cost_per_h_budget:
        alerts.append({"slo": "llm_spend_per_hour", "value": round(cost_usd / window_h, 4),
                       "target": cost_per_h_budget})
    if tokens / window_h > tokens_per_h_budget:
        alerts.append({"slo": "tokens_per_hour", "value": round(tokens / window_h), "target": tokens_per_h_budget})
    return alerts


EXPECTED_TOOLS = {
    "order_status": {"required": ["order_status"], "forbidden": ["issue_refund", "goodwill_credit"]},
    "refund": {"required": [], "forbidden": ["goodwill_credit"]},
    "policy": {"required": [], "forbidden": ["issue_refund", "goodwill_credit", "order_status"]},
    "escalate": {"required": [], "forbidden": ["issue_refund", "goodwill_credit", "order_status", "price"]},
    "price": {"required": ["price"], "forbidden": ["issue_refund"]},
}


def functional_eval(intent: str, tool_calls: list[str]) -> dict:
    """Online check that the right tools ran for an intent. Unknown intents fail closed."""
    spec = EXPECTED_TOOLS.get(intent)
    if spec is None:
        return {"pass": not tool_calls, "missing": [], "forbidden": list(tool_calls)}
    missing = [t for t in spec["required"] if t not in tool_calls]
    bad = [t for t in tool_calls if t in spec["forbidden"]]
    return {"pass": not missing and not bad, "missing": missing, "forbidden": bad}
