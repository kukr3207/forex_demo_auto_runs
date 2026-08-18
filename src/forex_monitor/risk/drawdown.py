"""Equity drawdown tracking without mutable global state."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence, Tuple

from forex_monitor.models import as_decimal


@dataclass(frozen=True)
class DrawdownPoint:
    equity: Decimal
    peak: Decimal
    drawdown: Decimal


def drawdown_series(values: Sequence[object]) -> Tuple[DrawdownPoint, ...]:
    if not values:
        return ()
    equities = tuple(as_decimal(value, "equity", positive=True) for value in values)
    peak = equities[0]
    result = []
    for equity in equities:
        peak = max(peak, equity)
        result.append(DrawdownPoint(equity, peak, (peak - equity) / peak))
    return tuple(result)


def maximum_drawdown(values: Sequence[object]) -> Decimal:
    points = drawdown_series(values)
    return max((point.drawdown for point in points), default=Decimal("0"))
