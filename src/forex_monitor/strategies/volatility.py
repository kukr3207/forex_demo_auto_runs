"""Volatility expansion and contraction classification."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from forex_monitor.analytics.indicators import average_true_range
from forex_monitor.models import Candle, Signal
from forex_monitor.strategies.base import StrategyDecision


@dataclass(frozen=True)
class VolatilityExpansionStrategy:
    period: int = 14
    expansion_ratio: Decimal = Decimal("1.25")

    @property
    def name(self) -> str:
        return "volatility_expansion"

    def evaluate(self, candles: Sequence[Candle]) -> StrategyDecision:
        if len(candles) < self.period * 2:
            raise ValueError("volatility strategy requires two ATR windows")
        atr = average_true_range(candles, self.period)
        current = atr[-1]
        previous = atr[-self.period]
        if current is None or previous is None or previous == 0:
            raise ValueError("ATR values are not ready")
        ratio = current / previous
        direction = candles[-1].close - candles[-1].open
        signal = Signal.NEUTRAL
        if ratio >= self.expansion_ratio:
            signal = (
                Signal.BUY if direction > 0 else Signal.SELL if direction < 0 else Signal.NEUTRAL
            )
        confidence = min(Decimal("1"), abs(ratio - Decimal("1")))
        return StrategyDecision(self.name, signal, confidence, ("atr_ratio",), {"ratio": ratio})
