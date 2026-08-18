"""Market-data consistency, continuity, and quality reporting."""

from forex_monitor.quality.candle_rules import validate_candles
from forex_monitor.quality.continuity import MissingInterval, missing_intervals
from forex_monitor.quality.issues import IssueSeverity, QualityIssue
from forex_monitor.quality.outliers import Outlier, detect_outliers, median_absolute_deviation
from forex_monitor.quality.quote_rules import validate_quotes
from forex_monitor.quality.service import DataQualityService, QualityReport
from forex_monitor.quality.summary import issue_counts, severity_counts

__all__ = [
    "DataQualityService",
    "IssueSeverity",
    "MissingInterval",
    "Outlier",
    "QualityIssue",
    "QualityReport",
    "detect_outliers",
    "issue_counts",
    "median_absolute_deviation",
    "missing_intervals",
    "severity_counts",
    "validate_candles",
    "validate_quotes",
]
