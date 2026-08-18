"""Deterministic strategy backtesting and walk-forward helpers."""

from forex_monitor.backtesting.engine import BacktestEngine
from forex_monitor.backtesting.execution import ExecutionModel
from forex_monitor.backtesting.ledger import SimulationLedger
from forex_monitor.backtesting.metrics import TradeMetrics, trade_metrics
from forex_monitor.backtesting.models import (
    BacktestResult,
    CompletedTrade,
    SimulatedFill,
    SimulatedOrder,
)
from forex_monitor.backtesting.optimization import (
    ParameterScore,
    best_score,
    parameter_grid,
    rank_scores,
)
from forex_monitor.backtesting.service import BacktestReport, BacktestService
from forex_monitor.backtesting.walk_forward import (
    WalkForwardWindow,
    expanding_windows,
    rolling_windows,
)

__all__ = [
    "BacktestEngine",
    "BacktestReport",
    "BacktestResult",
    "BacktestService",
    "CompletedTrade",
    "ExecutionModel",
    "ParameterScore",
    "SimulatedFill",
    "SimulatedOrder",
    "SimulationLedger",
    "TradeMetrics",
    "WalkForwardWindow",
    "best_score",
    "expanding_windows",
    "parameter_grid",
    "rank_scores",
    "rolling_windows",
    "trade_metrics",
]
