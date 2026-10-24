"""Talk 17: streaming through every hop. Socket tests run a fake model 20x faster than the story
and use loose tolerances; arithmetic tests are exact. See docs/streaming.md."""
import asyncio
import json
import pathlib
import re
import shutil

import fakeredis
import pytest

from sysdesign.clock import FakeClock
from sysdesign.ext import streaming as st

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCALE = 20.0
TTFT = 0.88 / SCALE
RATE = st.TOK_S * SCALE
FULL = 0.88 / SCALE + st.MEDIAN_TOKENS / RATE  # about 0.8 s


def run(coro):
    return asyncio.run(coro)


async def through(mode=None, buffered=False, accel=True, **get_kw):
    led = st.StreamLedger(fakeredis.FakeRedis())
    srv = await st.FakeModelServer(led, ttft_s=TTFT, tok_s=RATE, buffered=buffered, accel_header=accel).start()
    px = await st.MiniProxy(srv.port, mode=mode).start() if mode else None
    try:
        return await st.sse_get(px.port if px else srv.port, "t17-key-0001", **get_kw), srv
    finally:
        if px:
            await px.close()
        await srv.close()


# ---------------------------------------------------------------- arithmetic from the story
def test_generation_is_380_over_25():
    assert st.breakdown()["generation_s"] == pytest.approx(15.2)


def test_first_token_is_network_queue_prefill():
    assert st.first_token_s() == pytest.approx(0.88)


def test_blank_screen_is_about_16_1():
    assert st.breakdown()["buffered_blank_s"] == pytest.approx(16.08, abs=0.01)


def test_generation_dominates_the_wait():
    b = st.breakdown()
    assert b["generation_s"] / b["buffered_blank_s"] > 0.9


def test_answer_times_short_median_long():
    t = st.answer_times()
    assert t[380] == pytest.approx(16.08, abs=0.01) and t[1000] == pytest.approx(40.88, abs=0.01)
    assert t[120] < t[380] < t[1000]


def test_model_is_about_4_5x_reader():
    assert st.model_reader_ratio() == pytest.approx(4.55, abs=0.01)


def test_doubling_model_speed_only_widens_a_gap_the_reader_never_closes():
    assert st.model_reader_ratio(50) > st.model_reader_ratio(25) > 1


def test_reader_never_catches_the_model():
    for p in st.reader_curve():
        assert p["read"] <= p["shown"]


def test_nothing_on_screen_before_first_token():
    c = st.reader_curve(step=0.1, horizon_s=1.0)
    assert all(p["shown"] == 0 for p in c if p["t"] < 0.88)


def test_quoted_ab_is_labelled_quoted():
    assert "quoted" in st.QUOTED_AB["source"]
    assert st.QUOTED_AB["model_b"]["abandonment"] == 0.22 and st.QUOTED_AB["model_b"]["quality_delta"] == -6


# ---------------------------------------------------------------- transport choice
def test_sse_chosen_for_one_way_answers():
    assert st.choose_transport() == "sse"


def test_websocket_only_when_client_pushes():
    assert st.choose_transport(needs_client_push=True) == "websocket"


def test_only_sse_has_builtin_resume_and_plain_http():
    rows = {r["name"]: r for r in st.compare_transports()}
    assert rows["sse"]["builtin_resume"] and rows["sse"]["plain_http"]
    assert not rows["websocket"]["plain_http"] and rows["websocket"]["sticky_or_broker"]
    assert not rows["long_polling"]["builtin_resume"]


def test_poll_interval_sets_latency():
    assert st.poll_latency_s(1.0) == 0.5 and st.poll_latency_s(0.2) == 0.1


# ---------------------------------------------------------------- SSE encoding and progress
def test_sse_round_trip():
    evs, rest = st.parse_sse(st.sse_event(7, "token", "hi") + st.sse_event(8, "done", {"tokens": 1}) + b"id: 9")
    assert [e["id"] for e in evs] == [7, 8] and evs[0]["data"] == "hi" and rest == b"id: 9"


def test_sse_data_is_json_so_newlines_cannot_split_events():
    evs, _ = st.parse_sse(st.sse_event(1, "token", "a\n\nb"))
    assert len(evs) == 1 and evs[0]["data"] == "a\n\nb"


def test_progress_label_from_tool_event():
    ev = {"event": "tool_call", "data": {"tool": "order_status", "args": {"order_id": "9917"}}}
    assert st.progress_label(ev) == "Checking order 9917"


def test_progress_label_unknown_tool_is_generic():
    assert st.progress_label({"event": "tool_call", "data": {"tool": "x"}}) == "Working on it"


def test_progress_label_missing_arg_is_generic():
    assert st.progress_label({"event": "tool_call", "data": {"tool": "order_status", "args": {}}}) == "Working on it"


def test_progress_label_ignores_tokens():
    assert st.progress_label({"event": "token", "data": "x"}) is None


# ---------------------------------------------------------------- ledger
@pytest.fixture
def ledger():
    return st.StreamLedger(fakeredis.FakeRedis(), clock=FakeClock(), ttl_s=300)


def test_first_open_creates_second_does_not(ledger):
    first = ledger.open("k")
    second = ledger.open("k")
    assert first and not second


def test_after_returns_only_newer_events(ledger):
    ledger.open("k")
    for i in range(1, 6):
        ledger.append("k", i, "token", f"w{i}")
    assert [e[0] for e in ledger.after("k", 3)] == [4, 5]


def test_resume_plan_resumes(ledger):
    ledger.open("k")
    for i in range(1, 381):
        ledger.append("k", i, "token", i)
    plan = st.resume_plan(ledger, "k", 140)
    assert plan["action"] == "resume" and len(plan["events"]) == 240 and plan["events"][0][0] == 141


def test_expired_ledger_marks_truncated_and_offers_continue(ledger):
    ledger.open("k")
    ledger.append("k", 1, "token", "a")
    ledger.clock.advance(301)
    plan = st.resume_plan(ledger, "k", 140)
    assert plan["action"] == "truncated"
    eid, kind, data = plan["events"][0]
    assert eid == 141 and kind == "truncated" and data["offer"] == "continue"


def test_expired_ledger_has_no_state(ledger):
    ledger.open("k")
    ledger.clock.advance(301)
    assert ledger.state("k") is None and ledger.after("k", 0) == []


def test_unknown_key_is_expired(ledger):
    assert ledger.expired("never-opened")


def test_finish_sets_state(ledger):
    ledger.open("k")
    ledger.finish("k", "cancelled")
    assert ledger.state("k") == "cancelled"


# ---------------------------------------------------------------- measured: buffered vs streamed
def test_streamed_first_token_is_near_ttft():
    r, _ = run(through())
    assert r.tokens == 380 and r.first_token_s < TTFT + 0.15


def test_service_buffering_hides_the_stream():
    r, _ = run(through(buffered=True))
    assert r.first_token_s > FULL * 0.85


def test_gateway_buffering_hides_a_streaming_service():
    r, _ = run(through(mode="buffer"))
    assert r.first_token_s > FULL * 0.85


def test_gateway_gzip_without_flush_hides_the_stream():
    r, _ = run(through(mode="gzip"))
    assert r.tokens == 380 and r.first_token_s > FULL * 0.85


def test_pass_through_gateway_keeps_the_stream():
    r, _ = run(through(mode="pass"))
    assert r.first_token_s < TTFT + 0.15 and r.done_s > FULL * 0.85


def test_tool_events_arrive_before_first_token():
    r, _ = run(through(mode="pass"))
    assert r.first_event_s < r.first_token_s
    labels = [st.progress_label(e) for e in r.events if e.get("event") == "tool_call"]
    assert labels == ["Checking order 9917", "Issuing refund for order 9917"]


def test_stream_ends_with_done_event():
    r, _ = run(through(mode="pass"))
    assert r.ended == "done" and r.events[-1]["event"] == "done"


def test_event_ids_are_increasing():
    r, _ = run(through(mode="pass"))
    ids = [e["id"] for e in r.events]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)


# ---------------------------------------------------------------- drop, resume, cancel, stall
async def _drop(regenerate=False):
    led = st.StreamLedger(fakeredis.FakeRedis())
    srv = await st.FakeModelServer(led, ttft_s=TTFT, tok_s=RATE, grace_s=0.25,
                                   regenerate_on_reconnect=regenerate).start()
    px = await st.MiniProxy(srv.port).start()
    try:
        a = await st.sse_get(px.port, "drop-0001", drop_after_tokens=140)
        await asyncio.sleep(0.02)
        b = await st.sse_get(px.port, "drop-0001", last_id=0 if regenerate else a.last_id)
        await asyncio.sleep(0.05)
        return a, b, srv
    finally:
        await px.close()
        await srv.close()


def test_resume_gets_exactly_the_rest():
    a, b, srv = run(_drop())
    assert a.tokens == 140 and b.tokens == 240 and b.ended == "done"


def test_resume_never_regenerates():
    _, _, srv = run(_drop())
    assert sum(srv.stats.generated.values()) == 380 and srv.stats.starts == {"drop-0001": 1}


def test_resume_does_not_repeat_tool_calls():
    _, b, _ = run(_drop())
    assert not any(e.get("event") == "tool_call" for e in b.events)


def test_regenerate_costs_more_tokens_and_repeats_tool_calls():
    _, b, srv = run(_drop(regenerate=True))
    assert sum(srv.stats.generated.values()) >= 380 + 140 and srv.stats.starts["drop-0001"] == 2
    assert any(e.get("event") == "tool_call" for e in b.events)


async def _close(propagate):
    led = st.StreamLedger(fakeredis.FakeRedis())
    srv = await st.FakeModelServer(led, ttft_s=TTFT, tok_s=RATE, grace_s=0.05, propagate_cancel=propagate).start()
    px = await st.MiniProxy(srv.port, propagate_cancel=propagate).start()
    try:
        await st.sse_get(px.port, "close-0001", drop_after_tokens=140)
        await asyncio.sleep(FULL + 0.3)
        return srv
    finally:
        await px.close()
        await srv.close()


def test_cancel_propagation_stops_generation():
    srv = run(_close(True))
    gen = srv.stats.generated["close-0001"]
    assert srv.stats.cancelled.get("close-0001") and 140 <= gen < 260


def test_without_propagation_tokens_go_to_the_void():
    srv = run(_close(False))
    assert srv.stats.generated["close-0001"] == 380 and not srv.stats.cancelled


async def _stall():
    led = st.StreamLedger(fakeredis.FakeRedis())
    srv = await st.FakeModelServer(led, ttft_s=TTFT, tok_s=RATE, grace_s=2.0).start()
    px = await st.MiniProxy(srv.port, stall_after=3000).start()
    try:
        return await st.resilient_get(px.port, "stall-0001", idle_timeout_s=0.15), srv
    finally:
        await px.close()
        await srv.close()


def test_stalled_stream_times_out_and_resumes():
    r, srv = run(_stall())
    assert r["endings"][0] == "idle_timeout" and r["endings"][-1] == "done"
    assert r["tokens"] == 380 and srv.stats.starts == {"stall-0001": 1}


def test_reconnect_after_server_lost_task_reads_ledger():
    async def go():
        led = st.StreamLedger(fakeredis.FakeRedis())
        led.open("old-0002")
        for i in range(1, 11):
            led.append("old-0002", i, "token", f"w{i}")
        led.append("old-0002", 11, "done", {"tokens": 10})
        led.finish("old-0002")
        srv = await st.FakeModelServer(led, ttft_s=TTFT, tok_s=RATE).start()
        try:
            return await st.sse_get(srv.port, "old-0002", last_id=4), srv
        finally:
            await srv.close()
    r, srv = run(go())
    assert r.tokens == 6 and srv.stats.starts == {}


def test_server_sends_no_buffer_header():
    async def go():
        led = st.StreamLedger(fakeredis.FakeRedis())
        srv = await st.FakeModelServer(led, ttft_s=TTFT, tok_s=RATE, tokens=3).start()
        try:
            rd, wr = await asyncio.open_connection("127.0.0.1", srv.port)
            wr.write(b"GET /v1/stream/hdr-0001 HTTP/1.1\r\n\r\n")
            head = await rd.readuntil(b"\r\n\r\n")
            wr.close()
            return head.decode().lower()
        finally:
            await srv.close()
    h = run(go())
    assert "text/event-stream" in h and "x-accel-buffering: no" in h


# ---------------------------------------------------------------- gateway config (gzip conflict fix)
NGINX = (ROOT / "deploy/nginx.conf").read_text()


def test_repo_nginx_turns_proxy_buffering_off():
    assert re.search(r"proxy_buffering off;", NGINX)


def test_repo_nginx_gzip_never_lists_event_stream():
    types = re.search(r"gzip_types ([^;]+);", NGINX).group(1)
    assert "application/json" in types and "text/event-stream" not in types


def test_docs_no_longer_claim_gzip_on_the_stream():
    doc = (ROOT / "docs/networking-journey.md").read_text()
    assert "text/event-stream" in doc


def test_app_sse_routes_send_no_buffer_header():
    from sysdesign import app
    assert app.SSE_HEADERS["x-accel-buffering"] == "no" and "no-transform" in app.SSE_HEADERS["cache-control"]


@pytest.mark.skipif(not (shutil.which("nginx") or shutil.which("nginx", path="/usr/sbin")), reason="no nginx binary")
def test_real_nginx_buffering_off_keeps_stream():
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    import stream_replay as rp

    r = run(rp.one({"accel_header": False}, None, SCALE, nginx_body=rp.NGINX_VARIANTS["nginx_repo_rule"]))
    assert r["tokens"] == 380 and r["first_token_s"] < TTFT + 0.2


# ---------------------------------------------------------------- committed results
REPLAY = json.loads((ROOT / "docs/results/stream_replay.json").read_text())


def test_committed_replay_is_full_talk_speed():
    assert REPLAY["scale"] == 1.0 and REPLAY["model"]["tokens"] == 380


def test_committed_replay_buffered_vs_streamed():
    m = REPLAY["mini_proxy"]
    assert m["incident_service_and_gateway_buffered"]["first_token_s"] > 15
    assert m["streamed_end_to_end"]["first_token_s"] < 1.2


def test_committed_replay_resume_vs_regenerate():
    d = {x["mode"]: x for x in REPLAY["drop_at_140"]}
    assert d["resume_from_ledger"]["tokens_generated"] == 380
    assert d["regenerate"]["tokens_generated"] >= 520


def test_committed_replay_cancel_saves_tokens():
    c = {x["propagate_cancel"]: x for x in REPLAY["tab_close_at_140"]}
    assert c[True]["tokens_saved"] > 150 and c[False]["tokens_saved"] == 0


def test_streaming_results_are_deterministic():
    assert st.results() == st.results()
