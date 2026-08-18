"""Backtest execution and report composition facade."""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Sequence

from forex_monitor.backtesting.engine import BacktestEngine
from forex_monitor.backtesting.metrics import TradeMetrics, trade_metrics
from forex_monitor.backtesting.models import BacktestResult
from forex_monitor.models import Candle
from forex_monitor.strategies.base import Strategy


@dataclass(frozen=True)
class BacktestReport:
    result: BacktestResult
    metrics: TradeMetrics


@dataclass
class BacktestService:
    engine: BacktestEngine = field(default_factory=BacktestEngine)

    def evaluate(
        self,
        strategy: Strategy,
        candles: Sequence[Candle],
        initial_balance: object = Decimal("10000"),
        quantity: object = Decimal("1"),
        warmup: int = 30,
    ) -> BacktestReport:
        result = self.engine.run(strategy, candles, initial_balance, quantity, warmup)
        return BacktestReport(result, trade_metrics(result.trades))
