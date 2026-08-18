"""Pairwise return correlation for concentration checks."""

from decimal import Decimal
from typing import Mapping, Sequence, Tuple

from forex_monitor.analytics.indicators import mean, standard_deviation
from forex_monitor.models import as_decimal


def correlation(left: Sequence[object], right: Sequence[object]) -> Decimal:
    if len(left) != len(right) or len(left) < 2:
        raise ValueError("correlation series must have equal lengths of at least two")
    left_values = tuple(as_decimal(value, "left return") for value in left)
    right_values = tuple(as_decimal(value, "right return") for value in right)
    denominator = standard_deviation(left_values) * standard_deviation(right_values)
    left_mean = mean(left_values)
    right_mean = mean(right_values)
    covariance = sum(
        (
            (left_value - left_mean) * (right_value - right_mean)
            for left_value, right_value in zip(left_values, right_values)
        ),
        Decimal("0"),
    ) / Decimal(len(left_values))
    return Decimal("0") if denominator == 0 else covariance / denominator


def correlation_pairs(series: Mapping[str, Sequence[object]]) -> Mapping[Tuple[str, str], Decimal]:
    names = sorted(series)
    return {
        (left, right): correlation(series[left], series[right])
        for index, left in enumerate(names)
        for right in names[index + 1 :]
    }
