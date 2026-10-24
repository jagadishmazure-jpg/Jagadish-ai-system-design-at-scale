"""Worker autoscaler driven by where work waits (queue depth and oldest-task age), not CPU.

desired = max(by_depth, by_age), clamped to [warm_min, max_workers]
  by_depth = ceil(depth / target_jobs_per_worker)
  by_age   = current + step when the oldest job waited longer than max_age_s
Scale-up is immediate; scale-down waits for `scale_down_stable_s` of lower demand (no flapping)
and never drops below the warm minimum, because a cold worker takes time to start.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class Autoscaler:
    warm_min: int = 1
    max_workers: int = 20
    target_jobs_per_worker: int = 10
    max_age_s: float = 5.0
    age_step: int = 2
    scale_down_stable_s: float = 30.0
    current: int = 1
    _low_since: float | None = None
    history: list[tuple[float, int, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.current = max(self.current, self.warm_min)

    def decide(self, now: float, depth: int, oldest_age_s: float) -> int:
        by_depth = math.ceil(max(0, depth) / self.target_jobs_per_worker)
        by_age = self.current + self.age_step if oldest_age_s > self.max_age_s else 0
        desired = min(self.max_workers, max(self.warm_min, by_depth, by_age))
        if desired > self.current:
            self.current, self._low_since = desired, None
            self.history.append((now, desired, "up"))
        elif desired < self.current:
            if self._low_since is None:
                self._low_since = now
            elif now - self._low_since >= self.scale_down_stable_s:
                self.current, self._low_since = desired, None
                self.history.append((now, desired, "down"))
        else:
            self._low_since = None
        return self.current
