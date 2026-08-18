"""Immutable portfolio positions and their mark-to-market values."""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from forex_monitor.models import as_decimal, normalize_symbol


class PositionSide(str, Enum):
    LONG = "long"
    SHORT = "short"

    @property
    def multiplier(self) -> Decimal:
        return Decimal("1") if self is PositionSide.LONG else Decimal("-1")


@dataclass(frozen=True)
class Position:
    symbol: str
    side: PositionSide
    quantity: Decimal
    entry_price: Decimal
    current_price: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(self, "quantity", as_decimal(self.quantity, "quantity", positive=True))
        object.__setattr__(
            self, "entry_price", as_decimal(self.entry_price, "entry price", positive=True)
        )
        object.__setattr__(
            self,
            "current_price",
            as_decimal(self.current_price, "current price", positive=True),
        )

    @property
    def notional(self) -> Decimal:
        return self.quantity * self.current_price

    @property
    def unrealized_pnl(self) -> Decimal:
        change = self.current_price - self.entry_price
        return change * self.quantity * self.side.multiplier

    def mark(self, price: object) -> "Position":
        current = as_decimal(price, "current price", positive=True)
        return Position(self.symbol, self.side, self.quantity, self.entry_price, current)
