"""Worker autoscaler driven by queue depth and oldest-task age, not CPU.

desired = clamp(ceil(depth / jobs_per_worker), warm_min, max), plus one step
up whenever the oldest waiting job is older than the age target (work is
waiting even if depth looks small). Scale-down waits for a cooldown so a
brief lull does not throw away warm capacity; new workers only count once
their start-up delay has passed.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class Autoscaler:
    warm_min: int = 1
    max_workers: int = 12
    jobs_per_worker: int = 10
    max_oldest_age_s: float = 20.0
    scale_down_cooldown_s: float = 60.0
    startup_s: float = 5.0
    current: int = 0
    pending: list = field(default_factory=list)  # ready-at times of starting workers
    decisions: list = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.warm_min < 0 or self.max_workers < max(1, self.warm_min):
            raise ValueError("need 0 <= warm_min <= max_workers and max_workers >= 1")
        if self.jobs_per_worker <= 0:
            raise ValueError("jobs_per_worker must be positive")
        self.current = max(self.current, self.warm_min)
        self._last_up = -math.inf

    def desired(self, depth: int, oldest_age_s: float) -> int:
        want = math.ceil(max(0, depth) / self.jobs_per_worker)
        if oldest_age_s > self.max_oldest_age_s and depth > 0:
            want = max(want, self.current + len(self.pending) + 1)
        return max(self.warm_min, min(self.max_workers, want))

    def ready_workers(self, now: float) -> int:
        ready = [t for t in self.pending if t <= now]
        self.pending = [t for t in self.pending if t > now]
        self.current += len(ready)
        return self.current

    def step(self, now: float, depth: int, oldest_age_s: float) -> int:
        self.ready_workers(now)
        want = self.desired(depth, oldest_age_s)
        total = self.current + len(self.pending)
        if want > total:
            self.pending += [now + self.startup_s] * (want - total)
            self._last_up = now
            self.decisions.append((round(now, 1), "up", want))
        elif want < self.current and now - self._last_up >= self.scale_down_cooldown_s and not self.pending:
            self.current = want
            self.decisions.append((round(now, 1), "down", want))
        return self.current
