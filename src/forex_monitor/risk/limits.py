"""Pre-trade risk limits and observable decisions."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Tuple

from forex_monitor.models import as_decimal


@dataclass(frozen=True)
class RiskLimits:
    max_order_notional: Decimal
    max_gross_leverage: Decimal
    max_daily_loss: Decimal
    max_open_positions: int

    def __post_init__(self) -> None:
        for name in ("max_order_notional", "max_gross_leverage", "max_daily_loss"):
            object.__setattr__(self, name, as_decimal(getattr(self, name), name, positive=True))
        if self.max_open_positions < 1:
            raise ValueError("maximum open positions must be positive")


@dataclass(frozen=True)
class LimitDecision:
    allowed: bool
    reasons: Tuple[str, ...]


def evaluate_order(
    limits: RiskLimits,
    order_notional: object,
    projected_leverage: object,
    daily_pnl: object,
    open_positions: int,
) -> LimitDecision:
    reasons = []
    if as_decimal(order_notional, "order notional") > limits.max_order_notional:
        reasons.append("order_notional")
    if as_decimal(projected_leverage, "projected leverage") > limits.max_gross_leverage:
        reasons.append("gross_leverage")
    if as_decimal(daily_pnl, "daily PnL") < -limits.max_daily_loss:
        reasons.append("daily_loss")
    if open_positions >= limits.max_open_positions:
        reasons.append("open_positions")
    return LimitDecision(not reasons, tuple(reasons))
