# Latency: where the time goes and what to do about it

In main's load model the model call dominates end-to-end time, so a faster model or fewer model calls (cache, intent ladder) beats tuning the gateway. See [load-test.md](load-test.md) for those numbers; they are not repeated here.

Batching at a fixed offered load (simulated GPU server on one machine, `loadtest/results/talks_benchmarks.json`, produced by `python -m loadtest.talks_benchmarks`):
| Batch | p50 ms | p95 ms | Throughput rps |
|---|---|---|---|
| 1 | 510.2 | 976.49 | 195.0 |
| 4 | 62.59 | 111.28 | 639.4 |
| 16 | 19.2 | 27.07 | 844.3 |
At batch 1 the server cannot keep up, so the time is queueing, not compute. Batching raises throughput and that removes the queue.

## Decision tree
```mermaid
flowchart TD
  S[p95 too high] --> Q1{Which stage dominates?}
  Q1 -->|inference| Q2{Repeat questions?}
  Q2 -->|yes| C[semantic cache]
  Q2 -->|no| Q3{Easy turns on a big model?}
  Q3 -->|yes| R[router to small model]
  Q3 -->|no| ST[stream tokens: SSE, cut TTFT] --> BT[batching on self-hosted]
  Q1 -->|retrieval| RR{Reranker earning its cost?}
  RR -->|no| DROP[drop reranker - see rag-pipeline.md]
  Q1 -->|network| NC[keep-alive, TCP_NODELAY, region pinning - networking-journey.md]
  Q1 -->|queue wait| AS[autoscale on queue age, not CPU]
  NC --> N2{Far from the user? count round trips}
  N2 -->|handshakes| T13[TLS 1.3, keep-alive, DNS cache]
  N2 -->|slow start on big pages| IW[smaller page, bigger initial window]
  N2 -->|RTT itself| EDGE[edge server + warm origin link - network-waterfall.md]
  Q1 -->|event loop stalls| CB{CPU-bound step on the loop?}
  CB -->|yes| PP[process pool, not threads - concurrency.md]
  CB -->|no, waiting| AW[async or threads are fine]
```
