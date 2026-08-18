"""Composable trading strategies and ensemble evaluation."""

from forex_monitor.strategies.base import Strategy, StrategyDecision
from forex_monitor.strategies.breakout import ChannelBreakoutStrategy
from forex_monitor.strategies.ensemble import combine_decisions
from forex_monitor.strategies.filters import SessionFilter, filter_session, filter_spread
from forex_monitor.strategies.mean_reversion import ZScoreMeanReversionStrategy
from forex_monitor.strategies.momentum import RsiMomentumStrategy
from forex_monitor.strategies.registry import StrategyRegistry
from forex_monitor.strategies.service import StrategyService
from forex_monitor.strategies.trend import MovingAverageTrendStrategy
from forex_monitor.strategies.volatility import VolatilityExpansionStrategy

__all__ = [
    "ChannelBreakoutStrategy",
    "MovingAverageTrendStrategy",
    "RsiMomentumStrategy",
    "SessionFilter",
    "Strategy",
    "StrategyDecision",
    "StrategyRegistry",
    "StrategyService",
    "VolatilityExpansionStrategy",
    "ZScoreMeanReversionStrategy",
    "combine_decisions",
    "filter_session",
    "filter_spread",
]
