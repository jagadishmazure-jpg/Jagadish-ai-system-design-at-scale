# Talk 17: streaming answers through every hop

Source: a public talk on AI system design, credited by topic only and paraphrased in my own words. Nothing here quotes it.

**What is real and what is not.** The model is a fake. It waits 0.88 s and then emits 380 tokens at 25 a second, because those are the story's settings. Everything between the fake model and the client is real: real sockets on 127.0.0.1, a real gateway stand-in, real nginx 1.26.3, and wall-clock timing. So every time below is a *measured transport* time on top of a *simulated model*. The abandonment and quality figures come from the talk and are marked **quoted**. Measured numbers come from `python scripts/stream_replay.py` (about 4 minutes at full speed) and are stored in `docs/results/stream_replay.json`. The arithmetic is in `docs/results/talk17.json`, and CI fails if it drifts.

## The story (fictional)

Wren is the support assistant for the made-up Pinecrest Goods stores. People said it was "broken": they typed a question, nothing moved for about 16 seconds, and then the whole answer appeared at once. The talk says 38% left first (quoted). There were no errors, and the model provider looked healthy.

Two quick fixes came up. One was to buy a faster model. The other was to add a progress bar. The evidence below says neither one targets the cause.

![Streaming evidence board](img/stream_evidence_board.png)

## Where the 16 seconds go

| Piece | Time |
|---|---|
| Network | 0.08 s |
| Provider queue | 0.20 s |
| Prefill | 0.60 s |
| Generating 380 tokens at 25/s | 15.2 s |
| **Blank screen while buffered** | **16.08 s** |
| First token is ready at | 0.88 s |

The first token exists after 0.88 s. The user waits for the last one only because some hop holds everything back.

**Why a faster model is the wrong fix.** A reader takes in about 5.5 tokens a second. At 25 tokens a second the model already runs 4.55x faster than anyone reads. At 50 it runs 9.1x faster. The extra speed widens a gap the reader never closes (panel c). The talk's A/B test (quoted) found that a 2x faster model cut abandonment from 38% to 22% but cost 6 quality points. That is a partial gain with a real cost.

## Measured: time to first token, buffered vs streamed (scale 1.0)

| Path | First token | Last token |
|---|---|---|
| Service buffers and gateway buffers (the incident) | **16.05 s** | 16.05 s |
| Service streams, gateway buffers | 16.05 s | 16.05 s |
| Gateway gzips the stream without flushing (our stand-in) | 16.05 s | 16.05 s |
| Streamed end to end | **0.884 s** | 16.05 s |
| Real nginx, `proxy_buffering on` | 5.48 s | 16.05 s |
| Real nginx, `proxy_buffering off` | 0.884 s | 16.05 s |
| Real nginx, buffering off, gzip on `text/event-stream` | 0.883 s | 16.05 s |
| Real nginx, this repo's rule (gzip on JSON only) | 0.885 s | 16.05 s |
| Real nginx, buffering on, but the service sends `X-Accel-Buffering: no` | 0.885 s | 16.05 s |

How to read it:
- A single buffering hop is enough to hide the stream. Fixing only the service did nothing while the gateway still buffered.
- Real nginx with buffering on does not hold the whole answer. It forwards each time its proxy buffers fill, so the first token arrived at 5.48 s rather than 16 s. That is still a blank screen for most people.
- Real nginx 1.26 with buffering off did not delay the stream even with gzip on `text/event-stream`, because it flushes the compressor whenever the upstream flushes. Our stand-in gateway gzips the way a plain compressor does, without flushing, and it held everything. We exempt `text/event-stream` from gzip anyway, since not every hop flushes and compressing tiny events saves almost nothing.
- The header the service sends (`X-Accel-Buffering: no`) turns buffering off for that response even when the gateway default is on. Both settings are now in place.
- A streamed first token is about 4 ms above the model's 0.88 s. The first visible event (a tool-call progress line) shows up at 0.22 s.

## Why SSE

| | Long polling | WebSocket | SSE |
|---|---|---|---|
| Direction | The client keeps asking | Both ways | Server to client |
| Plain HTTP through proxies | Yes | No (upgrade, sticky session or broker) | Yes |
| Resume after a drop | No | Build it yourself | `Last-Event-ID` |
| Token latency | Half the poll interval on average | Per token | Per token |

A chat answer is one request up and many events down. The two-way channel of a WebSocket goes unused, and long polling ties latency to the poll interval. `choose_transport()` returns SSE unless the client really needs to push mid-answer.

## When the stream breaks

| Case | What happens | Measured (scale 1.0) |
|---|---|---|
| Mobile drop at token 140 of 380, reconnect with `Last-Event-ID: 142` | The server finds the request's stream in the ledger, sends events 143 and on, and never restarts generation | 140 + 240 tokens received, **380 generated**, 1 generation |
| The same drop, "fixed" by regenerating from the start | The tool calls run again and the first tokens are paid for twice | **532 generated**, 2 generations, tool calls repeated |
| Ledger entry expired (TTL 300 s) | A `truncated` event with an offer to continue; no silent restart | Tested with a fake clock |
| A hop stalls (stops forwarding, keeps the socket open) | The client's 2 s idle timeout fires, and it reconnects with `Last-Event-ID` | 2 connections, all 380 tokens, 1 generation |
| Tab closed at token 140, cancel passed upstream | The gateway closes upstream, the server waits a 1 s grace period for a reconnect, then stops | **166 generated, 214 saved** |
| Tab closed, no cancel passing | Tokens keep flowing to nobody | 380 generated, 0 saved |

The ledger is the request's idempotency key in Redis, the same one the job queue already uses. `StreamLedger` stores every event under `sd:stream:{key}:events` as it is produced, which is why a resume costs no extra tokens.

**Progress as events, not a bar.** A percentage bar says nothing true about an answer of unknown length. The stream carries the agent's tool calls as events (`tool_call`, `{"tool": "order_status", "args": {"order_id": "9917"}}`), and `progress_label()` turns them into "Checking order 9917" and "Issuing refund for order 9917". These arrive before the first token.

## Guardrails
- SSE data is JSON-encoded, so a token containing blank lines cannot split or forge an event (tested).
- Resume reads only the caller's own key. Keys are scoped `{tenant}:{user}:{key}` as in `/v1/refunds`.
- Event payloads carry order ids, never card numbers, emails, or phone numbers. The ledger expires after 300 s.
- The whole-turn deadline budget from talk 16 still applies. Idle timeout and grace are slices of it, not extra time.

## Limitations
- The model is a fake with a fixed speed. Real providers vary token to token, and the replay does not show that.
- The idle timeout here is 2 s (the talk used 10 s) to keep the replay short.
- The FastAPI `/v1/chat/stream` route still produces the full reply before streaming it. It now sends the no-buffer headers, but live token streaming from a real provider through that route is **planned**.
- The socket tests run at 20x speed with loose tolerances, and one test needs an `nginx` binary (it is skipped otherwise).

## Built vs planned

| Piece | Status |
|---|---|
| Incident arithmetic, reader-vs-model curve, transport comparison | Built, tested |
| SSE ledger with resume, truncated fallback, idle-timeout reconnect, cancel after grace | Built, tested (real sockets, fake model) |
| Gateway stand-in (buffer / gzip / pass / stall / cancel) and real-nginx replay | Built, measured |
| `deploy/nginx.conf`: gzip on JSON only, buffering off on `/v1/`; `SSE_HEADERS` on both app SSE routes | Built |
| Wiring `StreamLedger` into `/v1/chat/stream` with a real provider stream | Planned |
| Client-side UI for `truncated` and "continue" | Planned |

Run it: `python scripts/stream_replay.py && python scripts/talk17_bench.py && python scripts/stream_evidence_board.py`. Tests: `tests/test_talk17_streaming.py`.
