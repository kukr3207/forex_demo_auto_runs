"""Stable interfaces shared by trading strategies."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping, Protocol, Sequence, Tuple

from forex_monitor.models import Candle, Signal


@dataclass(frozen=True)
class StrategyDecision:
    strategy: str
    signal: Signal
    confidence: Decimal
    reasons: Tuple[str, ...]
    metadata: Mapping[str, object]

    def __post_init__(self) -> None:
        if not self.strategy.strip():
            raise ValueError("strategy name cannot be blank")
        if self.confidence < 0 or self.confidence > 1:
            raise ValueError("strategy confidence must be between zero and one")


class Strategy(Protocol):
    @property
    def name(self) -> str: ...

    def evaluate(self, candles: Sequence[Candle]) -> StrategyDecision: ...
