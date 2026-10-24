from sysdesign.cache import SemanticCache
from sysdesign.knowledge import KnowledgeIndex

Q = "what is the return window for electronics"


def test_hit_on_paraphrase_miss_on_different_question(r, clock):
    c = SemanticCache(r, threshold=0.8, clock=clock)
    c.put("retail", {"chat"}, "policy", Q, "30 days.", ["returns#0"])
    assert c.get("retail", {"chat"}, "policy", "what's the return window for electronics?")
    assert c.get("retail", {"chat"}, "policy", "do gift cards expire") is None


def test_never_caches_non_knowledge_intents(r, clock):
    c = SemanticCache(r, clock=clock)
    for intent in ("price", "order_status", "refund", "escalate"):
        assert not c.put("retail", {"chat"}, intent, Q, "x", ["returns#0"])
        assert c.get("retail", {"chat"}, intent, Q) is None
    assert c.size() == 0


def test_never_caches_questions_with_pii(r, clock):
    c = SemanticCache(r, clock=clock)
    assert not c.put("retail", {"chat"}, "policy", Q + " my email a@b.co", "x", ["returns#0"])


def test_tenant_and_permission_isolation(r, clock):
    c = SemanticCache(r, threshold=0.8, clock=clock)
    c.put("retail", {"chat"}, "policy", Q, "30 days.", ["returns#0"])
    assert c.get("wholesale", {"chat"}, "policy", Q) is None
    assert c.get("retail", {"chat", "staff"}, "policy", Q) is None
    assert c.get("retail", ["chat"], "policy", Q) is not None


def test_ttl_expiry(r, clock):
    c = SemanticCache(r, ttl_s=60, clock=clock)
    c.put("retail", {"chat"}, "policy", Q, "30 days.", ["returns#0"])
    clock.advance(61)
    assert c.get("retail", {"chat"}, "policy", Q) is None and c.size() == 0


def test_cached_answer_is_redacted(r, clock):
    c = SemanticCache(r, clock=clock)
    c.put("retail", {"chat"}, "policy", Q, "write to help@shop.example", ["returns#0"])
    assert "[EMAIL]" in c.get("retail", {"chat"}, "policy", Q)["answer"]


def test_cdc_reindexes_only_changed_chunk():
    idx = KnowledgeIndex()
    paras = list(idx.chunks[f"returns#{i}"].text for i in range(3))
    paras[0] = "Electronics can be returned within 7 days of delivery in original packaging."
    changed = idx.apply_change("returns", paras)
    assert changed == {"returns#0"} and idx.embed_calls == 1


def test_cdc_removed_paragraph_reported():
    idx = KnowledgeIndex()
    assert idx.apply_change("payments", [idx.chunks["payments#0"].text]) == {"payments#1"}
    assert "payments#1" not in idx.chunks


def test_cdc_invalidates_only_affected_entries(r, clock):
    c = SemanticCache(r, clock=clock)
    c.put("retail", {"chat"}, "policy", Q, "30 days.", ["returns#0"])
    c.put("retail", {"chat"}, "policy", "do gift cards expire", "No.", ["payments#1"])
    assert c.invalidate_chunks({"returns#0"}) == 1
    assert c.get("retail", {"chat"}, "policy", "do gift cards expire")
    assert c.get("retail", {"chat"}, "policy", Q) is None


def test_bad_threshold_rejected(r):
    import pytest
    with pytest.raises(ValueError):
        SemanticCache(r, threshold=0)
