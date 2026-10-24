"""Wire one in-process system (used by the API, tests, evals and the load harness)."""
from __future__ import annotations

from dataclasses import dataclass

import fakeredis

from .admission import AdmissionController, Limits
from .agent import Agent
from .approvals import ApprovalGate
from .cache import SemanticCache
from .clock import RealClock
from .events import OrderEvents
from .llm import default_providers
from .mcp import DomainServer, ToolClient, add_calculators
from .memory import EpisodicMemory, FactMemory
from .observability import Metrics, TraceStore
from .rag import KnowledgeBase
from .resilience import ModelGateway
from .store import Store
from .tools import build_registry


@dataclass
class System:
    agent: Agent
    admission: AdmissionController
    store: Store
    redis: object
    kb: KnowledgeBase
    cache: SemanticCache
    gateway: ModelGateway
    gate: ApprovalGate
    registry: object
    tools: ToolClient
    facts: FactMemory
    episodes: EpisodicMemory
    metrics: Metrics
    order_events: object = None


def build(redis=None, dsn: str = ":memory:", clock=None, limits: Limits | None = None, *, admission=True,
          cache=True, router=True, resilience=True, grounding=True, seed: int = 7, providers=None) -> System:
    clock = clock or RealClock()
    redis = redis if redis is not None else fakeredis.FakeRedis()
    store = Store(dsn)
    kb = KnowledgeBase()
    providers = providers or default_providers(seed)
    gw = ModelGateway(providers, resilience=resilience, clock=clock)
    reg = build_registry(kb, store)
    add_calculators(reg)
    tools = ToolClient([DomainServer(d, reg) for d in ("orders", "payments", "account")])
    gate = ApprovalGate(reg, store, clock)
    facts = FactMemory(clock)
    sc = SemanticCache(clock=clock, enabled=cache)
    metrics = Metrics()
    agent = Agent(gw, kb, sc, reg, gate, facts, metrics, TraceStore(), router_enabled=router, grounding=grounding)
    adm = AdmissionController(redis, limits or Limits(), clock=None, enabled=admission)
    return System(agent, adm, store, redis, kb, sc, gw, gate, reg, tools, facts, EpisodicMemory(), metrics, OrderEvents())
