"""Stop-loss and take-profit price calculations."""

from dataclasses import dataclass
from decimal import Decimal

from forex_monitor.models import as_decimal
from forex_monitor.portfolio.position import PositionSide


@dataclass(frozen=True)
class ExitLevels:
    stop_loss: Decimal
    take_profit: Decimal


def exit_levels(
    entry_price: object,
    side: PositionSide,
    stop_distance: object,
    reward_to_risk: object = Decimal("2"),
) -> ExitLevels:
    entry = as_decimal(entry_price, "entry price", positive=True)
    distance = as_decimal(stop_distance, "stop distance", positive=True)
    ratio = as_decimal(reward_to_risk, "reward to risk", positive=True)
    direction = side.multiplier
    stop = entry - distance * direction
    target = entry + distance * ratio * direction
    if stop <= 0 or target <= 0:
        raise ValueError("calculated exit prices must be positive")
    return ExitLevels(stop, target)


def atr_stop_distance(atr: object, multiplier: object = Decimal("2")) -> Decimal:
    return as_decimal(atr, "ATR", positive=True) * as_decimal(
        multiplier, "ATR multiplier", positive=True
    )
