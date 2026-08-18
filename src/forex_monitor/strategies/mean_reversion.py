"""Rolling z-score mean-reversion strategy."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from forex_monitor.analytics.indicators import z_scores
from forex_monitor.models import Candle, Signal
from forex_monitor.strategies.base import StrategyDecision


@dataclass(frozen=True)
class ZScoreMeanReversionStrategy:
    period: int = 20
    entry_threshold: Decimal = Decimal("2")

    @property
    def name(self) -> str:
        return "zscore_mean_reversion"

    def evaluate(self, candles: Sequence[Candle]) -> StrategyDecision:
        if len(candles) < self.period:
            raise ValueError("mean-reversion strategy requires more history")
        value = z_scores(tuple(candle.close for candle in candles), self.period)[-1]
        if value is None:
            raise ValueError("z-score is not ready")
        signal = (
            Signal.BUY
            if value <= -self.entry_threshold
            else Signal.SELL
            if value >= self.entry_threshold
            else Signal.NEUTRAL
        )
        confidence = min(Decimal("1"), abs(value) / (self.entry_threshold * Decimal("2")))
        return StrategyDecision(self.name, signal, confidence, ("zscore",), {"zscore": value})
