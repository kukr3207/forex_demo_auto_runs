"""Reusable trading-session and liquidity decision filters."""

from dataclasses import dataclass, replace
from datetime import time
from decimal import Decimal

from forex_monitor.models import Signal
from forex_monitor.strategies.base import StrategyDecision


@dataclass(frozen=True)
class SessionFilter:
    opens_at: time
    closes_at: time

    def allows(self, value: time) -> bool:
        if self.opens_at <= self.closes_at:
            return self.opens_at <= value < self.closes_at
        return value >= self.opens_at or value < self.closes_at


def filter_session(
    decision: StrategyDecision, session: SessionFilter, observed_at: time
) -> StrategyDecision:
    if session.allows(observed_at):
        return decision
    return replace(
        decision,
        signal=Signal.NEUTRAL,
        confidence=Decimal("0"),
        reasons=(*decision.reasons, "outside_session"),
    )


def filter_spread(
    decision: StrategyDecision, spread_ratio: object, maximum: object
) -> StrategyDecision:
    spread = Decimal(str(spread_ratio))
    limit = Decimal(str(maximum))
    if spread <= limit:
        return decision
    return replace(
        decision,
        signal=Signal.NEUTRAL,
        confidence=Decimal("0"),
        reasons=(*decision.reasons, "spread_limit"),
    )
