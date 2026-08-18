"""Price-channel breakout strategy."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from forex_monitor.models import Candle, Signal
from forex_monitor.strategies.base import StrategyDecision


@dataclass(frozen=True)
class ChannelBreakoutStrategy:
    lookback: int = 20

    @property
    def name(self) -> str:
        return "channel_breakout"

    def evaluate(self, candles: Sequence[Candle]) -> StrategyDecision:
        if self.lookback < 2 or len(candles) <= self.lookback:
            raise ValueError("breakout strategy requires a valid lookback and history")
        window = candles[-self.lookback - 1 : -1]
        upper = max(candle.high for candle in window)
        lower = min(candle.low for candle in window)
        close = candles[-1].close
        signal = Signal.BUY if close > upper else Signal.SELL if close < lower else Signal.NEUTRAL
        width = upper - lower
        confidence = (
            Decimal("0")
            if width == 0
            else min(Decimal("1"), abs(close - (upper + lower) / 2) / width)
        )
        return StrategyDecision(
            self.name,
            signal,
            confidence,
            ("channel_boundary",),
            {"upper": upper, "lower": lower},
        )
