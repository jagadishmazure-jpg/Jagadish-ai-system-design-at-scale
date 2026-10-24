# Talk 18 (C): concurrency is not parallelism

Source: a public talk on AI system design, credited by topic only and written in my own words. Everything on this page was **measured for real** on one machine: 8 CPUs, Python 3.13.5, GIL enabled (`docs/results/talk18_measured.json`, produced by `python scripts/talk18_bench.py --measure`). Your numbers will differ. CI runs smaller versions with loose bounds (`tests/test_talk18_concurrency.py`) and never compares raw timings.

## The story (fictional)

Brightwell Learning's chat app ran for a year on one asyncio loop and once held 10,000+ open chats without trouble. Then a "send a photo" feature shipped. It shrinks the image and boosts contrast, and it was written quickly and barely tested. On launch day about 100 people uploaded at once, every chat froze, and CPU hit 100%.

The trap is the bigger number. 10,000 chats sounds harder than 100 uploads, but they are different kinds of load.

## Two kinds of work

- **I/O-bound:** the task mostly waits (for the network, a disk or a model API). A chat connection spends nearly all of its life idle.
- **CPU-bound:** the task keeps a core busy the whole time. Resizing an image in Python never waits.

**Concurrency** means many tasks are *in progress*, taking turns on one worker. **Parallelism** means many tasks *run at the same instant* on several cores. An event loop gives you concurrency: while one task waits, another runs. It cannot give you parallelism, and a task that never waits never hands the loop back.

**A kitchen, in our words.** One cook with ten orders puts pasta on to boil, chops salad while it cooks, and plates dessert while the oven heats. That is concurrency, and it works because most steps are waiting. Now give the same cook ten cakes to ice by hand. There is nothing to wait on, so juggling does not help. You need more cooks at more counters, which is parallelism. In CPython, threads are extra cooks who share **one knife**: the GIL lets only one thread run Python code at a time. Processes are cooks with their own counter and their own knife.

## The incident, replayed (real sockets, real processes)

`sysdesign/ext/concurrency.py` starts an asyncio server, opens 2,000 idle connections to it, and runs a heartbeat task that wakes every 5 ms and records how late it was. Then 100 uploads arrive, each a pure-Python 640x480 shrink-and-contrast job (about 31 ms each on this box).

| | Worst heartbeat stall | Chat ping during uploads | 100 uploads done in | Throughput |
|---|---|---|---|---|
| 2,000 idle chats, no uploads | 0.3 ms | n/a | n/a | n/a |
| Uploads processed **on the loop** (the bug) | **2,960.4 ms** | 2,965.8 ms (only 1 ping got through) | 2.966 s | 33.7 jobs/s |
| Uploads sent to a **process pool** (8 workers) | **5.8 ms** | p50 0.1 ms, worst 5.7 ms (44 pings) | 0.469 s | 213.1 jobs/s |

The fix is one line: `await loop.run_in_executor(process_pool, image_preprocess, seed)`. With it the loop keeps serving chats and the work spreads over 8 cores, about 6.3x the throughput.

## The talk's experiments, on this machine

**5,000 tasks that each sleep 50 ms** (pure waiting):

| | Time |
|---|---|
| Thread pool, 256 threads | 1.037 s (about 20 batches of 256, so close to the ideal 1.0 s) |
| asyncio, 5,000 coroutines | 0.073 s |

**150 CPU tasks (count primes below 20,000):**

| | Time | Speed-up vs serial |
|---|---|---|
| Serial | 1.496 s | 1.0x |
| 24 threads | 1.542 s | 0.97x (the GIL: no gain) |
| asyncio | 1.530 s | about 1.0x (nothing ever waits, so nothing interleaves) |
| Process pool, 8 processes | 0.240 s | **6.24x** |

## The `await` on a plain value mistake

```python
async def handler():
    n = await count_primes(10_000)   # TypeError: object int can't be used in 'await' expression
```

`count_primes` is an ordinary function. It runs to completion, blocking the loop, and only then does `await` fail on the `int` it returned. Adding `async def` in front does not help either: a coroutine with no `await` inside still runs straight through. `test_async_def_without_await_never_yields` shows such tasks run strictly in order. The real fix is `await loop.run_in_executor(pool, count_primes, 10_000)`.

## Does `asyncio.to_thread(agent.handle, ...)` in `sysdesign/app.py` block?

We checked, and the honest answer is not today, but it could:

- One `agent.handle` turn takes about 11.3 ms here. Most of that is the fake provider's simulated wait, which releases the GIL. With 40 turns running through `to_thread`, the loop's worst stall was 18.0 ms.
- If `handle` ever does real CPU work in Python, such as local embeddings, big regex scans or document parsing, threads stop helping. With 40 image-sized CPU jobs (about 31 ms each) in `to_thread`, the loop's worst stall rose to **323.1 ms** (p99 114.2 ms). The loop thread has to win the GIL back from busy workers, and CPython only switches every 5 ms.
- `to_thread` uses the default executor, which has `min(32, CPUs + 4)` threads: 12 here. More concurrent turns than that wait in a queue (`test_default_to_thread_pool_is_small`).

No change was made to `app.py` in this PR. The guidance is in the [decision tree](latency.md): keep `handle` I/O-bound, and send any CPU step to a process pool.

## Interview points

- Ask what the work *does*, not how much of it there is. Waiting work: async or threads. CPU work: processes, or native code that releases the GIL.
- Prove it with a heartbeat. The loop's lateness is a direct measure of blocking.
- More threads will not speed up CPU work under the GIL. More processes on more cores will.
