"""Facade for pre-trade sizing, limits, and exit levels."""

from dataclasses import dataclass
from decimal import Decimal

from forex_monitor.portfolio.position import PositionSide
from forex_monitor.risk.limits import LimitDecision, RiskLimits, evaluate_order
from forex_monitor.risk.sizing import fixed_fractional_size
from forex_monitor.risk.stops import ExitLevels, exit_levels


@dataclass(frozen=True)
class RiskPlan:
    quantity: Decimal
    exits: ExitLevels
    decision: LimitDecision


@dataclass
class RiskService:
    limits: RiskLimits

    def plan(
        self,
        equity: object,
        risk_fraction: object,
        entry_price: object,
        stop_distance: object,
        side: PositionSide,
        projected_leverage: object,
        daily_pnl: object,
        open_positions: int,
    ) -> RiskPlan:
        quantity = fixed_fractional_size(equity, risk_fraction, stop_distance)
        exits = exit_levels(entry_price, side, stop_distance)
        decision = evaluate_order(
            self.limits,
            quantity * Decimal(str(entry_price)),
            projected_leverage,
            daily_pnl,
            open_positions,
        )
        return RiskPlan(quantity, exits, decision)
