"""Clock-injected operation timing and latency summaries."""

from dataclasses import dataclass, field
from statistics import median
from time import monotonic
from typing import Callable, List, Mapping


@dataclass
class LatencyTracker:
    clock: Callable[[], float] = monotonic
    samples: List[float] = field(default_factory=list)

    def measure(self, operation: Callable[[], object]) -> object:
        started = self.clock()
        try:
            return operation()
        finally:
            elapsed = self.clock() - started
            if elapsed < 0:
                raise ValueError("latency clock cannot move backwards")
            self.samples.append(elapsed)

    def summary(self) -> Mapping[str, float]:
        if not self.samples:
            return {"count": 0.0, "minimum": 0.0, "maximum": 0.0, "median": 0.0}
        return {
            "count": float(len(self.samples)),
            "minimum": min(self.samples),
            "maximum": max(self.samples),
            "median": float(median(self.samples)),
        }

    def reset(self) -> None:
        self.samples.clear()
