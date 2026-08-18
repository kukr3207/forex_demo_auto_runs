"""Single-position simulation ledger with explicit cash accounting."""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Optional, Tuple

from forex_monitor.backtesting.models import CompletedTrade, SimulatedFill
from forex_monitor.portfolio.position import PositionSide


@dataclass
class SimulationLedger:
    balance: Decimal
    open_fill: Optional[SimulatedFill] = None
    trades: List[CompletedTrade] = field(default_factory=list)

    def open(self, fill: SimulatedFill) -> None:
        if self.open_fill is not None:
            raise ValueError("a simulated position is already open")
        self.balance -= fill.commission
        self.open_fill = fill

    def close(self, fill: SimulatedFill) -> CompletedTrade:
        if self.open_fill is None:
            raise ValueError("there is no simulated position to close")
        entry = self.open_fill
        if fill.order.symbol != entry.order.symbol:
            raise ValueError("exit symbol must match the open position")
        multiplier = Decimal("1") if entry.order.side is PositionSide.LONG else Decimal("-1")
        pnl = (fill.price - entry.price) * entry.order.quantity * multiplier
        pnl -= fill.commission
        trade = CompletedTrade(entry, fill, pnl)
        self.balance += pnl
        self.trades.append(trade)
        self.open_fill = None
        return trade

    def completed(self) -> Tuple[CompletedTrade, ...]:
        return tuple(self.trades)
