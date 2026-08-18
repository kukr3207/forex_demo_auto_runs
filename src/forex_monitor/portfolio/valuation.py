"""Deterministic position and portfolio valuation helpers."""

from dataclasses import replace
from decimal import Decimal
from typing import Mapping, Sequence, Tuple

from forex_monitor.models import as_decimal, normalize_symbol
from forex_monitor.portfolio.position import Position


def mark_positions(
    positions: Sequence[Position], prices: Mapping[str, object]
) -> Tuple[Position, ...]:
    normalized = {
        normalize_symbol(symbol): as_decimal(value, "market price", positive=True)
        for symbol, value in prices.items()
    }
    marked = []
    for position in positions:
        price = normalized.get(position.symbol)
        marked.append(replace(position, current_price=price) if price is not None else position)
    return tuple(marked)


def total_notional(positions: Sequence[Position]) -> Decimal:
    return sum((position.notional for position in positions), Decimal("0"))


def total_unrealized_pnl(positions: Sequence[Position]) -> Decimal:
    return sum((position.unrealized_pnl for position in positions), Decimal("0"))


def gross_leverage(positions: Sequence[Position], equity: object) -> Decimal:
    available = as_decimal(equity, "equity", positive=True)
    return total_notional(positions) / available
