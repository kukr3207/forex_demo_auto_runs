"""Small in-process counter and gauge registry."""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, Mapping

from forex_monitor.models import as_decimal


@dataclass
class MetricRegistry:
    _counters: Dict[str, int] = field(default_factory=dict)
    _gauges: Dict[str, Decimal] = field(default_factory=dict)

    def increment(self, name: str, amount: int = 1) -> int:
        normalized = self._name(name)
        if amount < 0:
            raise ValueError("counter increments cannot be negative")
        self._counters[normalized] = self._counters.get(normalized, 0) + amount
        return self._counters[normalized]

    def gauge(self, name: str, value: object) -> Decimal:
        normalized = self._name(name)
        self._gauges[normalized] = as_decimal(value, "gauge value")
        return self._gauges[normalized]

    def snapshot(self) -> Mapping[str, object]:
        return {
            "counters": dict(sorted(self._counters.items())),
            "gauges": {name: str(value) for name, value in sorted(self._gauges.items())},
        }

    @staticmethod
    def _name(value: str) -> str:
        name = value.strip().lower().replace(" ", "_")
        if not name:
            raise ValueError("metric name cannot be blank")
        return name
