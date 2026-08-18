"""Account balance, margin, and equity snapshots."""

from dataclasses import dataclass
from decimal import Decimal

from forex_monitor.models import as_decimal


@dataclass(frozen=True)
class AccountSnapshot:
    balance: Decimal
    unrealized_pnl: Decimal = Decimal("0")
    used_margin: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        balance = as_decimal(self.balance, "balance")
        used_margin = as_decimal(self.used_margin, "used margin")
        if balance < 0 or used_margin < 0:
            raise ValueError("balance and used margin cannot be negative")
        object.__setattr__(self, "balance", balance)
        object.__setattr__(
            self, "unrealized_pnl", as_decimal(self.unrealized_pnl, "unrealized PnL")
        )
        object.__setattr__(self, "used_margin", used_margin)

    @property
    def equity(self) -> Decimal:
        return self.balance + self.unrealized_pnl

    @property
    def free_margin(self) -> Decimal:
        return self.equity - self.used_margin

    @property
    def margin_level(self) -> Decimal:
        if self.used_margin == 0:
            return Decimal("Infinity")
        return self.equity / self.used_margin * Decimal("100")
