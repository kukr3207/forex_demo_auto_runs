"""Foreign-exchange ingestion, analysis, alerting, and reporting tools.

The package intentionally keeps its core dependency-free. Network access,
storage, clocks, and identifier generation are represented by small protocol
interfaces so production code and deterministic tests can use the same service
objects.
"""

from forex_monitor.config import AppConfig, DatabaseConfig, ProviderConfig, RuntimeConfig
from forex_monitor.models import (
    Alert,
    AlertRule,
    Candle,
    Instrument,
    MarketSnapshot,
    Quote,
    Signal,
    Timeframe,
    normalize_symbol,
)

__all__ = [
    "Alert",
    "AlertRule",
    "AppConfig",
    "Candle",
    "DatabaseConfig",
    "Instrument",
    "MarketSnapshot",
    "ProviderConfig",
    "Quote",
    "RuntimeConfig",
    "Signal",
    "Timeframe",
    "normalize_symbol",
]

__version__ = "1.0.0"
