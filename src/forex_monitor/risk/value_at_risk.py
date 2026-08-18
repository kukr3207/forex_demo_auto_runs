"""Historical value-at-risk and expected-shortfall estimators."""

from decimal import Decimal
from typing import Sequence

from forex_monitor.models import as_decimal


def historical_var(returns: Sequence[object], confidence: object = Decimal("0.95")) -> Decimal:
    values = sorted(as_decimal(value, "return") for value in returns)
    level = as_decimal(confidence, "confidence", positive=True)
    if not values:
        raise ValueError("returns cannot be empty")
    if level >= 1:
        raise ValueError("confidence must be less than one")
    index = max(0, int((Decimal("1") - level) * Decimal(len(values))) - 1)
    return max(Decimal("0"), -values[index])


def expected_shortfall(returns: Sequence[object], confidence: object = Decimal("0.95")) -> Decimal:
    values = sorted(as_decimal(value, "return") for value in returns)
    threshold = -historical_var(values, confidence)
    tail = [value for value in values if value <= threshold]
    if not tail:
        return Decimal("0")
    return max(Decimal("0"), -sum(tail, Decimal("0")) / Decimal(len(tail)))
