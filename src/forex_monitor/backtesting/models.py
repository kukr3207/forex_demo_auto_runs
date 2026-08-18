"""Immutable backtest orders, fills, trades, and results."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Tuple

from forex_monitor.models import as_decimal, ensure_utc, normalize_symbol
from forex_monitor.portfolio.position import PositionSide


@dataclass(frozen=True)
class SimulatedOrder:
    symbol: str
    side: PositionSide
    quantity: Decimal
    created_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(self, "quantity", as_decimal(self.quantity, "quantity", positive=True))
        object.__setattr__(self, "created_at", ensure_utc(self.created_at))


@dataclass(frozen=True)
class SimulatedFill:
    order: SimulatedOrder
    price: Decimal
    commission: Decimal
    filled_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "price", as_decimal(self.price, "fill price", positive=True))
        object.__setattr__(self, "commission", as_decimal(self.commission, "commission"))
        object.__setattr__(self, "filled_at", ensure_utc(self.filled_at))
        if self.commission < 0:
            raise ValueError("commission cannot be negative")


@dataclass(frozen=True)
class CompletedTrade:
    entry: SimulatedFill
    exit: SimulatedFill
    pnl: Decimal


@dataclass(frozen=True)
class BacktestResult:
    initial_balance: Decimal
    final_balance: Decimal
    trades: Tuple[CompletedTrade, ...]
    equity_curve: Tuple[Decimal, ...]

    @property
    def net_profit(self) -> Decimal:
        return self.final_balance - self.initial_balance
