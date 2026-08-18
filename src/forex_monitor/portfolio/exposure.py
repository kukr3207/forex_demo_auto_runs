"""Currency and directional exposure aggregation."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, Sequence, Tuple

from forex_monitor.portfolio.position import Position, PositionSide


@dataclass(frozen=True)
class CurrencyExposure:
    currency: str
    amount: Decimal


def currency_exposures(positions: Sequence[Position]) -> Tuple[CurrencyExposure, ...]:
    totals: Dict[str, Decimal] = {}
    for position in positions:
        base = position.symbol[:3]
        quote = position.symbol[3:6]
        direction = Decimal("1") if position.side is PositionSide.LONG else Decimal("-1")
        totals[base] = totals.get(base, Decimal("0")) + position.quantity * direction
        quote_amount = position.quantity * position.current_price * -direction
        totals[quote] = totals.get(quote, Decimal("0")) + quote_amount
    return tuple(CurrencyExposure(currency, totals[currency]) for currency in sorted(totals))


def largest_exposure(positions: Sequence[Position]) -> CurrencyExposure:
    exposures = currency_exposures(positions)
    if not exposures:
        raise ValueError("at least one position is required")
    return max(exposures, key=lambda item: abs(item.amount))
