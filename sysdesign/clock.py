"""Injectable clocks so the same components run in real time (service) and virtual time (load model)."""
from __future__ import annotations

import time


class RealClock:
    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(max(0.0, seconds))


class FakeClock:
    """Manually advanced clock; sleep() advances time instantly."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.t = start

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += max(0.0, seconds)

    def advance(self, seconds: float) -> None:
        self.t += seconds
