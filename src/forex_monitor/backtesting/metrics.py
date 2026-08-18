"""Summary metrics calculated from completed simulated trades."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from forex_monitor.backtesting.models import CompletedTrade


@dataclass(frozen=True)
class TradeMetrics:
    count: int
    winners: int
    losers: int
    gross_profit: Decimal
    gross_loss: Decimal
    profit_factor: Decimal
    win_rate: Decimal


def trade_metrics(trades: Sequence[CompletedTrade]) -> TradeMetrics:
    profits = [trade.pnl for trade in trades if trade.pnl > 0]
    losses = [trade.pnl for trade in trades if trade.pnl < 0]
    gross_profit = sum(profits, Decimal("0"))
    gross_loss = -sum(losses, Decimal("0"))
    count = len(trades)
    profit_factor = Decimal("Infinity") if gross_loss == 0 else gross_profit / gross_loss
    win_rate = Decimal("0") if count == 0 else Decimal(len(profits)) / Decimal(count)
    return TradeMetrics(
        count,
        len(profits),
        len(losses),
        gross_profit,
        gross_loss,
        profit_factor,
        win_rate,
    )
