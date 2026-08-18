"""Compose quality rules into one public audit result."""

from dataclasses import dataclass
from typing import Sequence, Tuple

from forex_monitor.models import Candle, Quote
from forex_monitor.quality.candle_rules import validate_candles
from forex_monitor.quality.continuity import MissingInterval, missing_intervals
from forex_monitor.quality.issues import IssueSeverity, QualityIssue
from forex_monitor.quality.quote_rules import validate_quotes


@dataclass(frozen=True)
class QualityReport:
    issues: Tuple[QualityIssue, ...]
    gaps: Tuple[MissingInterval, ...]

    @property
    def valid(self) -> bool:
        return not any(issue.severity is IssueSeverity.ERROR for issue in self.issues)


@dataclass
class DataQualityService:
    def audit(self, quotes: Sequence[Quote] = (), candles: Sequence[Candle] = ()) -> QualityReport:
        issues = (*validate_quotes(quotes), *validate_candles(candles))
        return QualityReport(issues, missing_intervals(candles))
