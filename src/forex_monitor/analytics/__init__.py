"""Pure market analytics and database-backed analysis orchestration."""

from forex_monitor.analytics.aggregation import aggregate_quotes, resample_candles
from forex_monitor.analytics.indicators import (
    average_true_range,
    bollinger_bands,
    exponential_moving_average,
    macd,
    relative_strength_index,
    simple_moving_average,
)
from forex_monitor.analytics.performance import PerformanceSummary, summarize_returns
from forex_monitor.analytics.service import AnalysisService, SignalPolicy

__all__ = [
    "AnalysisService",
    "PerformanceSummary",
    "SignalPolicy",
    "aggregate_quotes",
    "average_true_range",
    "bollinger_bands",
    "exponential_moving_average",
    "macd",
    "relative_strength_index",
    "resample_candles",
    "simple_moving_average",
    "summarize_returns",
]
