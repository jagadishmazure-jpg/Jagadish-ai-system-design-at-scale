"""Talk 15: caching for LLM apps (part 1). All simulated; see docs/caching.md."""
import math

import fakeredis
import pytest

from sysdesign.cache import ResponseCache, SemanticCache, normalize_prompt
from sysdesign.ext import llm_cache as lc
from sysdesign.knowledge import embed


@pytest.fixture(scope="module")
def incident():
    return lc.replay_incident(1800)


# ---- 1. expected cost formula and curve
def test_expected_cost_formula():
    assert lc.expected_cost(0.25, 1.0, 5.0) == pytest.approx(0.25 * 1 + 0.75 * 5)


@pytest.mark.parametrize("h", [-0.01, 1.01, 2, math.nan, None, True])
def test_expected_cost_rejects_bad_hit_rate(h):
    with pytest.raises(ValueError):
        lc.expected_cost(h, 1.0, 2.0)


@pytest.mark.parametrize("hit,miss", [(-1, 2), (1, -2), (math.nan, 1)])
def test_expected_cost_rejects_negative_costs(hit, miss):
    with pytest.raises(ValueError):
        lc.expected_cost(0.5, hit, miss)


def test_expected_cost_endpoints():
    assert lc.expected_cost(0, 1, 9) == 9 and lc.expected_cost(1, 1, 9) == 1


def test_cost_curve_monotonic_and_linear():
    pts = lc.cost_curve(1.0, 3.0, steps=4)
    assert [h for h, _ in pts] == [0, 0.25, 0.5, 0.75, 1.0]
    costs = [c for _, c in pts]
    assert costs == sorted(costs, reverse=True)
    assert costs[2] == pytest.approx(2.0)


def test_cost_curve_rejects_zero_steps():
    with pytest.raises(ValueError):
        lc.cost_curve(1, 2, steps=0)


def test_curve_svg_is_valid_markup():
    svg = lc.curve_svg(lc.cost_curve(1, 2))
    assert svg.startswith("<svg") and svg.strip().endswith("</svg>") and "<path" in svg


# ---- 2. provider prefix cache simulator
def test_below_1024_tokens_never_cached():
    c = lc.PrefixCacheSim()
    seg = [("sys", 1000), ("q", 20)]
    c.call(seg, 0)
    assert c.call(seg, 1).cached_tokens == 0


def test_cached_tokens_rounded_to_128_blocks():
    c = lc.PrefixCacheSim()
    c.call([("sys", 4200), ("q1", 50)], 0)
    r = c.call([("sys", 4200), ("q2", 50)], 1)
    assert r.cached_tokens == 4096


def test_ttl_expires_after_5_minutes():
    c = lc.PrefixCacheSim()
    c.call([("sys", 2048), ("a", 10)], 0)
    assert c.call([("sys", 2048), ("b", 10)], 301).cached_tokens == 0


def test_ttl_rolls_on_each_hit():
    c = lc.PrefixCacheSim()
    c.call([("sys", 2048), ("a", 10)], 0)
    for t in (250, 500, 750):  # each within 5 minutes of the previous hit
        assert c.call([("sys", 2048), (f"q{t}", 10)], t).cached_tokens == 2048


def test_cached_tokens_billed_at_ten_percent():
    full = lc.call_cost(10_000, 0, 0)
    cached = lc.call_cost(10_000, 10_000, 0)
    assert cached == pytest.approx(full * 0.10)


def test_call_cost_rejects_cached_above_input():
    with pytest.raises(ValueError):
        lc.call_cost(100, 200, 0)


def test_prefix_must_match_from_the_first_token():
    c = lc.PrefixCacheSim()
    c.call([("ts:1", 15), ("sys", 4200)], 0)
    assert c.call([("ts:2", 15), ("sys", 4200)], 1).cached_tokens == 0


def test_ttft_drops_with_cached_tokens():
    assert lc.simulated_ttft(6900, 4096) < lc.simulated_ttft(6900, 0)


def test_negative_segment_rejected():
    with pytest.raises(ValueError):
        lc.PrefixCacheSim().call([("x", -1)], 0)


# ---- 3. prompt builder
def _parts(ts="100"):
    return lc.PromptParts(ts, "m1", ("sys", 4200), [("d1", 600)], [("h1", 300)], ("q", 50))


def test_timestamp_first_layout_order():
    seg = lc.build_prompt(_parts(), "timestamp_first")
    assert seg[0][0].startswith("ts:") and seg[2] == ("sys", 4200)


def test_stable_first_layout_order():
    ids = [s[0] for s in lc.build_prompt(_parts(), "stable_first")]
    assert ids == ["sys", "merchant:m1", "d1", "h1", "q", "ts:100"]


def test_both_layouts_same_token_count():
    a, b = (lc.build_prompt(_parts(), k) for k in ("timestamp_first", "stable_first"))
    assert sum(t for _, t in a) == sum(t for _, t in b)


def test_unknown_layout_rejected():
    with pytest.raises(ValueError):
        lc.build_prompt(_parts(), "random")


def test_stable_first_caches_across_seconds_timestamp_first_does_not():
    for layout, expect in (("timestamp_first", 0), ("stable_first", 4096)):
        c = lc.PrefixCacheSim()
        c.call(lc.build_prompt(_parts("100"), layout), 100)
        assert c.call(lc.build_prompt(_parts("101"), layout), 101).cached_tokens >= expect
        if expect == 0:
            assert c.call(lc.build_prompt(_parts("102"), layout), 102).cached_tokens == 0


# ---- 4. incident replay
def test_traffic_grew_1_3x(incident):
    assert incident["traffic_growth"] == pytest.approx(1.3, abs=0.01)


def test_bill_grew_much_faster_than_traffic(incident):
    assert incident["bill_growth"] > 2 * incident["traffic_growth"]


def test_prompt_grew_with_4200_static_tokens(incident):
    assert lc.AUGUST.system_tokens == 4200
    assert 6500 < incident["august_timestamp_first"]["avg_input_tokens"] < 7300
    assert 1600 < incident["june"]["avg_input_tokens"] < 2000


def test_wrong_hypothesis_longer_user_messages_ruled_out(incident):
    assert incident["hypotheses"]["users_send_longer_messages"]["ruled_out"]


def test_wrong_hypothesis_price_rise_ruled_out(incident):
    assert incident["hypotheses"]["provider_raised_prices"]["ruled_out"]


def test_timestamp_first_hits_only_from_same_second(incident):
    a = incident["august_timestamp_first"]
    hits = round(a["request_hit_rate"] * a["calls"])
    assert a["hits_from_same_second_same_merchant"] == hits
    assert a["hits_from_retries"] == a["retries"]  # every retry hit, nothing else across seconds


def test_timestamp_first_is_spiky(incident):
    a = incident["august_timestamp_first"]
    assert a["seconds_with_zero_hits"] / a["seconds_with_traffic"] > 0.5


def test_stable_first_is_steady(incident):
    s = incident["august_stable_first"]
    assert s["seconds_with_zero_hits"] == 0 and s["request_hit_rate"] > 0.99


def test_fix_raises_token_hit_rate(incident):
    assert incident["august_stable_first"]["token_hit_rate"] > 5 * incident["august_timestamp_first"][
        "token_hit_rate"]


def test_fix_cuts_input_cost_by_over_half(incident):
    assert incident["fix"]["input_cost_reduction"] > 0.5


def test_fix_lowers_monthly_bill(incident):
    assert incident["fix"]["monthly_bill_after"] < 0.6 * incident["fix"]["monthly_bill_before"]


def test_fix_lowers_ttft(incident):
    assert incident["fix"]["ttft_p50_after"] < 0.6 * incident["fix"]["ttft_p50_before"]


def test_replay_is_deterministic():
    assert lc.replay(lc.AUGUST, "stable_first", 300) == lc.replay(lc.AUGUST, "stable_first", 300)


def test_replay_scales_to_200k_turns_per_day():
    r = lc.replay(lc.AUGUST, "stable_first", 864)
    assert r["turns_simulated"] == 2000


# ---- 5. multi-turn
def test_append_only_beats_rewrite():
    assert lc.multi_turn("append_only")["cached_share"] > lc.multi_turn("rewrite")["cached_share"] + 0.2


def test_rewrite_still_caches_system_prefix():
    per = lc.multi_turn("rewrite")["cached_per_turn"]
    assert per[0] == 0 and all(x == 4096 for x in per[1:])


def test_summarise_keeps_system_prefix_cached():
    r = lc.multi_turn("summarise")
    assert r["summaries"] >= 1
    assert all(x >= 4096 for x in r["cached_per_turn"][1:])


def test_append_only_cached_grows_each_turn():
    per = lc.multi_turn("append_only")["cached_per_turn"]
    assert per[2:] == sorted(per[2:]) and per[-1] > per[2]


def test_multi_turn_bad_mode():
    with pytest.raises(ValueError):
        lc.multi_turn("shuffle")


# ---- 6. exact-match response cache
@pytest.mark.parametrize("a,b", [
    ("What is your refund policy?", "what is   your refund policy"),
    ("How long does shipping take??", "how long does shipping take."),
    ("  Do you ship internationally ", "do you ship internationally?"),
])
def test_normalization_merges_surface_variants(a, b):
    assert normalize_prompt(a) == normalize_prompt(b)


@pytest.mark.parametrize("a,b", [
    ("Where is order #A-1042?", "where is order #A-1043?"),
    ("status of SKU-77B", "status of SKU-77b"),
    ("refund 12.50", "refund 1250"),
])
def test_normalization_never_merges_different_ids(a, b):
    assert normalize_prompt(a) != normalize_prompt(b)


def test_normalization_keeps_id_with_trailing_mark():
    assert normalize_prompt("Where is order #A-1042?") == "where is order #A-1042"


def test_response_cache_aside_hit_and_tenant_isolation():
    rc = ResponseCache(fakeredis.FakeRedis())
    calls = []
    def compute():
        calls.append(1)
        return "Returns within 30 days."
    assert rc.get_or_compute("t1", "policy", "Refund policy?", compute) == ("Returns within 30 days.", False)
    assert rc.get_or_compute("t1", "policy", "refund policy", compute)[1] is True
    assert rc.get_or_compute("t2", "policy", "refund policy", compute)[1] is False
    assert len(calls) == 2


def test_response_cache_24h_ttl():
    r = fakeredis.FakeRedis()
    rc = ResponseCache(r)
    rc.get_or_compute("t1", "policy", "refund policy", lambda: "x")
    assert 86000 < r.ttl(rc.key("t1", "refund policy")) <= 86400


def test_response_cache_refuses_order_status_and_pii():
    rc = ResponseCache(fakeredis.FakeRedis())
    rc.get_or_compute("t1", "order_status", "where is order #A-1", lambda: "shipped")
    rc.get_or_compute("t1", "policy", "email me at ann@example.com the policy", lambda: "x")
    assert rc.stats["refused"] == 2 and rc.stats["stores"] == 0


def test_response_cache_rejects_bad_ttl():
    with pytest.raises(ValueError):
        ResponseCache(fakeredis.FakeRedis(), ttl_s=0)


def test_existing_semantic_cache_still_works():
    sc = SemanticCache(fakeredis.FakeRedis())
    sc.put("t1", {"chat"}, "policy", "what is the refund policy", "30 days", ["p#1"])
    assert sc.get("t1", {"chat"}, "policy", "what is the refund policy") is not None


def test_chat_benchmark_hit_rates():
    b = lc.chat_benchmark(fakeredis.FakeRedis, n=1500)
    assert b["exact_hit_rate_policy_only"] > 0.9
    assert b["exact_plus_semantic_hit_rate"] >= b["exact_hit_rate"]
    assert b["order_status_cached"] == 0


# ---- 7. write-through embedding cache
def test_embedding_cache_reindex_hit_rate():
    r = lc.reindex_benchmark(docs=50, chunks_per_doc=4, changed_docs=5)
    assert r["first_index_embed_calls"] == 200 and r["reindex_embed_calls"] == 10
    assert r["reindex_hit_rate"] == pytest.approx(0.95)


def test_embedding_key_changes_with_text_and_model():
    assert lc.text_key("a") != lc.text_key("a ") and lc.text_key("a") != lc.text_key("a", "embed-v2")


# ---- 8. cache-aside vs write-through
def test_cache_aside_vs_write_through():
    # cache-aside: the reader writes after a miss; first request is cold; stale until TTL
    r = fakeredis.FakeRedis()
    rc = ResponseCache(r)
    computes = []
    def answer(v):
        def f():
            computes.append(v)
            return v
        return f
    assert rc.get_or_compute("t", "policy", "refund policy", answer("30 days"))[1] is False  # cold start
    assert computes == ["30 days"]
    # source changes to 14 days; cache-aside keeps serving the old answer until TTL or explicit invalidation
    assert rc.get_or_compute("t", "policy", "refund policy", answer("14 days"))[0] == "30 days"
    stale_window = r.ttl(rc.key("t", "refund policy"))
    assert stale_window > 0
    # write-through: the writer (indexer) fills the cache, so the first read is warm and never stale
    ec = lc.EmbeddingCache(embed)
    ec.index(["refund within 14 days"])
    assert ec.lookup("refund within 14 days") is not None  # first reader pays nothing
    assert ec.lookup("refund within 30 days") is None  # old text can't be served for new text


def test_committed_results_match_a_fresh_run():
    import subprocess
    import sys
    out = subprocess.run([sys.executable, "scripts/caching_bench.py", "--check"], capture_output=True, text=True)
    assert out.returncode == 0, out.stdout


def test_readme_talk15_numbers_match_results():
    import json
    import pathlib
    r = json.loads(pathlib.Path("docs/results/caching.json").read_text())["incident"]
    readme = pathlib.Path("README.md").read_text()
    assert f"{r['bill_growth']}x" in readme and f"{r['fix']['input_cost_reduction'] * 100:.1f}%" in readme
    assert f"{r['fix']['ttft_p50_before']}s to {r['fix']['ttft_p50_after']}s" in readme
