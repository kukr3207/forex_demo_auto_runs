"""Relative-strength momentum strategy."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from forex_monitor.analytics.indicators import relative_strength_index
from forex_monitor.models import Candle, Signal
from forex_monitor.strategies.base import StrategyDecision


@dataclass(frozen=True)
class RsiMomentumStrategy:
    period: int = 14
    oversold: Decimal = Decimal("30")
    overbought: Decimal = Decimal("70")

    @property
    def name(self) -> str:
        return "rsi_momentum"

    def evaluate(self, candles: Sequence[Candle]) -> StrategyDecision:
        if len(candles) <= self.period:
            raise ValueError("momentum strategy requires more candle history")
        value = relative_strength_index(tuple(candle.close for candle in candles), self.period)[-1]
        if value is None:
            raise ValueError("RSI is not ready")
        signal = (
            Signal.BUY
            if value <= self.oversold
            else Signal.SELL
            if value >= self.overbought
            else Signal.NEUTRAL
        )
        confidence = min(Decimal("1"), abs(value - Decimal("50")) / Decimal("50"))
        return StrategyDecision(self.name, signal, confidence, ("rsi_level",), {"rsi": value})
