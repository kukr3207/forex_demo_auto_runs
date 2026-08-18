"""Compose stored candles and pure indicators into directional analysis."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Mapping, Optional, Sequence, Tuple

from forex_monitor.analytics.indicators import (
    average_true_range,
    bollinger_bands,
    exponential_moving_average,
    macd,
    relative_strength_index,
)
from forex_monitor.errors import AnalyticsError
from forex_monitor.models import (
    AnalysisResult,
    Candle,
    IndicatorValue,
    Signal,
    Timeframe,
)
from forex_monitor.storage import CandleRepository


@dataclass(frozen=True)
class SignalPolicy:
    """Thresholds used to convert indicator votes into a signal."""

    strong_buy_score: Decimal = Decimal("60")
    buy_score: Decimal = Decimal("20")
    sell_score: Decimal = Decimal("-20")
    strong_sell_score: Decimal = Decimal("-60")
    overbought_rsi: Decimal = Decimal("70")
    oversold_rsi: Decimal = Decimal("30")

    def __post_init__(self) -> None:
        if not (
            Decimal("-100")
            <= self.strong_sell_score
            < self.sell_score
            < self.buy_score
            < self.strong_buy_score
            <= Decimal("100")
        ):
            raise ValueError("signal score thresholds must increase between -100 and 100")
        if not Decimal("0") < self.oversold_rsi < self.overbought_rsi < Decimal("100"):
            raise ValueError("RSI thresholds must increase between 0 and 100")

    def signal(self, score: Decimal) -> Signal:
        if score >= self.strong_buy_score:
            return Signal.STRONG_BUY
        if score >= self.buy_score:
            return Signal.BUY
        if score <= self.strong_sell_score:
            return Signal.STRONG_SELL
        if score <= self.sell_score:
            return Signal.SELL
        return Signal.NEUTRAL


@dataclass
class AnalysisService:
    candles: CandleRepository
    policy: SignalPolicy = SignalPolicy()

    def analyze(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        limit: int = 200,
        at: Optional[datetime] = None,
    ) -> AnalysisResult:
        history = self.candles.range(symbol, timeframe, end=at, limit=limit)
        return self.analyze_candles(history)

    def analyze_candles(self, candles: Sequence[Candle]) -> AnalysisResult:
        if len(candles) < 35:
            raise AnalyticsError("analysis requires at least 35 candles")
        symbol = candles[0].symbol
        timeframe = candles[0].timeframe
        if any(candle.symbol != symbol or candle.timeframe != timeframe for candle in candles):
            raise AnalyticsError("analysis candles must share a symbol and timeframe")
        closes = tuple(candle.close for candle in candles)
        ema_fast = exponential_moving_average(closes, 12)[-1]
        ema_slow = exponential_moving_average(closes, 26)[-1]
        rsi_value = relative_strength_index(closes, 14)[-1]
        macd_value = macd(closes)[-1]
        atr_value = average_true_range(candles, 14)[-1]
        bands = bollinger_bands(closes, 20)[-1]
        if (
            ema_fast is None
            or ema_slow is None
            or rsi_value is None
            or atr_value is None
            or macd_value is None
            or bands is None
        ):
            raise AnalyticsError("analysis indicators are not ready")
        values: Mapping[str, Decimal] = {
            "ema_fast": ema_fast,
            "ema_slow": ema_slow,
            "rsi": rsi_value,
            "macd": macd_value.line,
            "macd_signal": macd_value.signal or Decimal("0"),
            "macd_histogram": macd_value.histogram or Decimal("0"),
            "atr": atr_value,
            "bollinger_middle": bands.middle,
            "bollinger_upper": bands.upper,
            "bollinger_lower": bands.lower,
            "bollinger_percent_b": bands.percent_b or Decimal("0.5"),
        }
        score, reasons = self._score(closes[-1], values)
        observed_at = candles[-1].closed_at
        indicators = tuple(
            IndicatorValue(
                symbol=symbol,
                name=name,
                timeframe=timeframe,
                observed_at=observed_at,
                value=value,
            )
            for name, value in values.items()
        )
        return AnalysisResult(
            symbol=symbol,
            timeframe=timeframe,
            observed_at=observed_at,
            indicators=indicators,
            signal=self.policy.signal(score),
            score=score,
            reasons=reasons,
        )

    def _score(
        self,
        close: Decimal,
        values: Mapping[str, Decimal],
    ) -> Tuple[Decimal, Tuple[str, ...]]:
        votes = []
        reasons = []
        if values["ema_fast"] > values["ema_slow"]:
            votes.append(Decimal("30"))
            reasons.append("fast EMA is above slow EMA")
        elif values["ema_fast"] < values["ema_slow"]:
            votes.append(Decimal("-30"))
            reasons.append("fast EMA is below slow EMA")
        if values["rsi"] <= self.policy.oversold_rsi:
            votes.append(Decimal("25"))
            reasons.append("RSI is oversold")
        elif values["rsi"] >= self.policy.overbought_rsi:
            votes.append(Decimal("-25"))
            reasons.append("RSI is overbought")
        if values["macd_histogram"] > 0:
            votes.append(Decimal("25"))
            reasons.append("MACD histogram is positive")
        elif values["macd_histogram"] < 0:
            votes.append(Decimal("-25"))
            reasons.append("MACD histogram is negative")
        if close < values["bollinger_lower"]:
            votes.append(Decimal("20"))
            reasons.append("price is below the lower Bollinger band")
        elif close > values["bollinger_upper"]:
            votes.append(Decimal("-20"))
            reasons.append("price is above the upper Bollinger band")
        score = max(Decimal("-100"), min(Decimal("100"), sum(votes, Decimal("0"))))
        return score, tuple(reasons or ("indicators are balanced",))
