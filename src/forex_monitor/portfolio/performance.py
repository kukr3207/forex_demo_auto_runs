"""Portfolio contribution and concentration calculations."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence, Tuple

from forex_monitor.portfolio.position import Position
from forex_monitor.portfolio.valuation import total_notional, total_unrealized_pnl


@dataclass(frozen=True)
class PositionContribution:
    symbol: str
    notional_weight: Decimal
    pnl_weight: Decimal


def position_contributions(positions: Sequence[Position]) -> Tuple[PositionContribution, ...]:
    notional = total_notional(positions)
    pnl = total_unrealized_pnl(positions)
    return tuple(
        PositionContribution(
            position.symbol,
            Decimal("0") if notional == 0 else position.notional / notional,
            Decimal("0") if pnl == 0 else position.unrealized_pnl / pnl,
        )
        for position in positions
    )


def concentration_index(positions: Sequence[Position]) -> Decimal:
    contributions = position_contributions(positions)
    return sum(
        (contribution.notional_weight**2 for contribution in contributions),
        Decimal("0"),
    )
