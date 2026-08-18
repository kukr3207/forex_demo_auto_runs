"""Evaluate registered strategies individually or as an ensemble."""

from dataclasses import dataclass
from typing import Sequence, Tuple

from forex_monitor.models import Candle
from forex_monitor.strategies.base import StrategyDecision
from forex_monitor.strategies.ensemble import combine_decisions
from forex_monitor.strategies.registry import StrategyRegistry


@dataclass
class StrategyService:
    registry: StrategyRegistry

    def evaluate(
        self, candles: Sequence[Candle], names: Sequence[str]
    ) -> Tuple[StrategyDecision, ...]:
        if not names:
            raise ValueError("at least one strategy name is required")
        return tuple(self.registry.get(name).evaluate(candles) for name in names)

    def ensemble(self, candles: Sequence[Candle], names: Sequence[str]) -> StrategyDecision:
        return combine_decisions(self.evaluate(candles, names))
