"""Median absolute-deviation outlier detection for prices."""

from dataclasses import dataclass
from decimal import Decimal
from statistics import median
from typing import Sequence, Tuple

from forex_monitor.models import as_decimal


@dataclass(frozen=True)
class Outlier:
    index: int
    value: Decimal
    score: Decimal


def median_absolute_deviation(values: Sequence[object]) -> Decimal:
    numbers = tuple(as_decimal(value, "outlier value") for value in values)
    if not numbers:
        raise ValueError("outlier values cannot be empty")
    center = Decimal(median(numbers))
    return Decimal(median(abs(value - center) for value in numbers))


def detect_outliers(
    values: Sequence[object], threshold: object = Decimal("3.5")
) -> Tuple[Outlier, ...]:
    numbers = tuple(as_decimal(value, "outlier value") for value in values)
    limit = as_decimal(threshold, "outlier threshold", positive=True)
    if not numbers:
        return ()
    center = Decimal(median(numbers))
    deviation = median_absolute_deviation(numbers)
    if deviation == 0:
        return ()
    scale = Decimal("0.6745")
    return tuple(
        Outlier(index, value, scale * (value - center) / deviation)
        for index, value in enumerate(numbers)
        if abs(scale * (value - center) / deviation) > limit
    )
