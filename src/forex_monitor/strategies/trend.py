"""Moving-average trend-following strategy."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from forex_monitor.analytics.indicators import exponential_moving_average
from forex_monitor.models import Candle, Signal
from forex_monitor.strategies.base import StrategyDecision


@dataclass(frozen=True)
class MovingAverageTrendStrategy:
    fast_period: int = 12
    slow_period: int = 26

    @property
    def name(self) -> str:
        return "moving_average_trend"

    def evaluate(self, candles: Sequence[Candle]) -> StrategyDecision:
        if self.fast_period >= self.slow_period or len(candles) < self.slow_period:
            raise ValueError("trend strategy requires valid periods and enough candles")
        closes = tuple(candle.close for candle in candles)
        fast = exponential_moving_average(closes, self.fast_period)[-1]
        slow = exponential_moving_average(closes, self.slow_period)[-1]
        if fast is None or slow is None:
            raise ValueError("trend averages are not ready")
        spread = (fast - slow) / slow
        signal = Signal.BUY if spread > 0 else Signal.SELL if spread < 0 else Signal.NEUTRAL
        confidence = min(Decimal("1"), abs(spread) * Decimal("100"))
        return StrategyDecision(self.name, signal, confidence, ("ema_spread",), {"spread": spread})
