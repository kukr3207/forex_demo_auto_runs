"""Deterministic spread, slippage, and commission simulation."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from forex_monitor.backtesting.models import SimulatedFill, SimulatedOrder
from forex_monitor.models import as_decimal
from forex_monitor.portfolio.position import PositionSide


@dataclass(frozen=True)
class ExecutionModel:
    spread: Decimal = Decimal("0")
    slippage: Decimal = Decimal("0")
    commission_rate: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        for name in ("spread", "slippage", "commission_rate"):
            value = as_decimal(getattr(self, name), name)
            if value < 0:
                raise ValueError(f"{name} cannot be negative")
            object.__setattr__(self, name, value)

    def fill(self, order: SimulatedOrder, mid_price: object, filled_at: datetime) -> SimulatedFill:
        mid = as_decimal(mid_price, "mid price", positive=True)
        direction = Decimal("1") if order.side is PositionSide.LONG else Decimal("-1")
        price = mid + direction * (self.spread / Decimal("2") + self.slippage)
        commission = order.quantity * price * self.commission_rate
        return SimulatedFill(order, price, commission, filled_at)
