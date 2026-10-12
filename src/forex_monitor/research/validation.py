"""Purged time-series cross-validation and aggregate scoring."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Callable, Mapping, Optional, Sequence, Tuple, TypeVar

from forex_monitor.errors import ValidationError
from forex_monitor.models import as_decimal, ensure_utc


T = TypeVar("T")


@dataclass(frozen=True)
class TimeSeriesFold:
    fold: int
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime

    def __post_init__(self) -> None:
        if self.fold <= 0:
            raise ValidationError("fold number must be positive")
        values = tuple(ensure_utc(item, "fold boundary") for item in (
            self.train_start, self.train_end, self.test_start, self.test_end,
        ))
        if not values[0] <= values[1] < values[2] <= values[3]:
            raise ValidationError("fold boundaries must order train before test")


@dataclass(frozen=True)
class FoldResult:
    fold: int
    train_count: int
    test_count: int
    metrics: Mapping[str, Decimal]


class WalkForwardValidator:
    def __init__(self, *, folds: int = 5, purge: timedelta = timedelta(0), anchored: bool = True) -> None:
        if folds <= 0:
            raise ValidationError("fold count must be positive")
        if purge.total_seconds() < 0:
            raise ValidationError("purge interval cannot be negative")
        self.folds = folds
        self.purge = purge
        self.anchored = anchored

    def split(self, timestamps: Sequence[datetime]) -> Tuple[TimeSeriesFold, ...]:
        values = tuple(sorted({ensure_utc(item, "validation timestamp") for item in timestamps}))
        if len(values) < self.folds + 2:
            raise ValidationError("insufficient timestamps for requested fold count")
        test_size = max(1, len(values) // (self.folds + 1))
        result = []
        for fold in range(1, self.folds + 1):
            test_index = len(values) - (self.folds - fold + 1) * test_size
            test_start = values[test_index]
            test_end_index = min(test_index + test_size - 1, len(values) - 1)
            test_end = values[test_end_index]
            eligible = [item for item in values[:test_index] if item < test_start - self.purge]
            if not eligible:
                raise ValidationError("purge interval leaves a fold without training data")
            train_end = eligible[-1]
            train_start = values[0] if self.anchored else eligible[max(0, len(eligible) - test_size * 2)]
            result.append(TimeSeriesFold(fold, train_start, train_end, test_start, test_end))
        return tuple(result)

    def evaluate(
        self,
        items: Sequence[T],
        timestamp: Callable[[T], datetime],
        evaluator: Callable[[Sequence[T], Sequence[T]], Mapping[str, object]],
    ) -> Tuple[FoldResult, ...]:
        folds = self.split([timestamp(item) for item in items])
        result = []
        for fold in folds:
            train = [item for item in items if fold.train_start <= ensure_utc(timestamp(item)) <= fold.train_end]
            test = [item for item in items if fold.test_start <= ensure_utc(timestamp(item)) <= fold.test_end]
            metrics = {name: as_decimal(value, name) for name, value in evaluator(train, test).items()}
            result.append(FoldResult(fold.fold, len(train), len(test), metrics))
        return tuple(result)

    @staticmethod
    def aggregate(results: Sequence[FoldResult]) -> Mapping[str, Decimal]:
        names = sorted({name for result in results for name in result.metrics})
        aggregated = {}
        for name in names:
            values = [result.metrics[name] for result in results if name in result.metrics]
            aggregated[name + "_mean"] = sum(values, Decimal("0")) / Decimal(len(values))
            aggregated[name + "_min"] = min(values)
            aggregated[name + "_max"] = max(values)
        return aggregated
