"""Injectable clocks so time-based logic (buckets, breakers, TTLs) is testable."""
from __future__ import annotations

import time


class RealClock:
    def now(self) -> float:
        return time.monotonic()

    def wall(self) -> float:
        return time.time()


class FakeClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.t = start

    def now(self) -> float:
        return self.t

    def wall(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s
