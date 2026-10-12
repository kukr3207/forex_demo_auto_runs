"""Service-level indicators, rolling objectives, and error budgets."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, Iterable, Mapping, Optional, Sequence, Tuple

from forex_monitor.errors import ValidationError
from forex_monitor.models import as_decimal, ensure_utc


@dataclass(frozen=True)
class ServiceLevelObjective:
    objective_id: str
    service: str
    indicator: str
    target: Decimal
    window: timedelta
    minimum_events: int = 1

    def __post_init__(self) -> None:
        if not self.objective_id.strip() or not self.service.strip() or not self.indicator.strip():
            raise ValidationError("SLO fields cannot be blank")
        target = as_decimal(self.target, "SLO target", positive=True)
        if target > 1:
            raise ValidationError("SLO target cannot exceed one")
        if self.window.total_seconds() <= 0 or self.minimum_events <= 0:
            raise ValidationError("SLO window and minimum events must be positive")
        object.__setattr__(self, "target", target)


@dataclass(frozen=True)
class IndicatorEvent:
    service: str
    indicator: str
    occurred_at: datetime
    good: bool
    weight: Decimal = Decimal("1")

    def __post_init__(self) -> None:
        object.__setattr__(self, "occurred_at", ensure_utc(self.occurred_at, "indicator event time"))
        object.__setattr__(self, "weight", as_decimal(self.weight, "indicator weight", positive=True))


@dataclass(frozen=True)
class SloEvaluation:
    objective_id: str
    start: datetime
    end: datetime
    total_events: int
    good_weight: Decimal
    total_weight: Decimal
    achieved: Optional[Decimal]
    target: Decimal
    error_budget: Decimal
    budget_remaining: Decimal

    @property
    def met(self) -> Optional[bool]:
        return self.achieved >= self.target if self.achieved is not None else None

    @property
    def burn_rate(self) -> Optional[Decimal]:
        if self.achieved is None or self.error_budget == 0:
            return None
        consumed = Decimal("1") - self.achieved
        return consumed / self.error_budget


class SloMonitor:
    def __init__(self) -> None:
        self._objectives: Dict[str, ServiceLevelObjective] = {}
        self._events: list[IndicatorEvent] = []

    def add(self, objective: ServiceLevelObjective) -> None:
        if objective.objective_id in self._objectives:
            raise ValidationError("SLO id already exists")
        self._objectives[objective.objective_id] = objective

    def record(self, event: IndicatorEvent) -> None:
        self._events.append(event)

    def evaluate(self, objective_id: str, now: datetime) -> SloEvaluation:
        try:
            objective = self._objectives[objective_id]
        except KeyError as exc:
            raise ValidationError("SLO was not found") from exc
        end = ensure_utc(now, "SLO evaluation time")
        start = end - objective.window
        events = [
            event for event in self._events
            if event.service == objective.service and event.indicator == objective.indicator
            and start < event.occurred_at <= end
        ]
        total = sum((event.weight for event in events), Decimal("0"))
        good = sum((event.weight for event in events if event.good), Decimal("0"))
        achieved = good / total if len(events) >= objective.minimum_events and total else None
        budget = Decimal("1") - objective.target
        remaining = budget if achieved is None else budget - (Decimal("1") - achieved)
        return SloEvaluation(objective_id, start, end, len(events), good, total, achieved, objective.target, budget, remaining)

    def alerts(self, now: datetime, burn_threshold: object = Decimal("1")) -> Tuple[SloEvaluation, ...]:
        threshold = as_decimal(burn_threshold, "burn threshold", positive=True)
        values = []
        for objective_id in self._objectives:
            evaluation = self.evaluate(objective_id, now)
            if evaluation.burn_rate is not None and evaluation.burn_rate > threshold:
                values.append(evaluation)
        return tuple(sorted(values, key=lambda item: item.burn_rate or Decimal("0"), reverse=True))
