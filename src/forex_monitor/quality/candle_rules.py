"""Candle consistency and completeness checks."""

from typing import Sequence, Tuple

from forex_monitor.models import Candle
from forex_monitor.quality.issues import IssueSeverity, QualityIssue


def validate_candles(candles: Sequence[Candle]) -> Tuple[QualityIssue, ...]:
    issues = []
    for candle in candles:
        record_id = f"{candle.symbol}:{candle.opened_at.isoformat()}"
        if not candle.complete:
            issues.append(
                QualityIssue(
                    "incomplete_candle",
                    "candle does not contain the expected samples",
                    IssueSeverity.INFO,
                    record_id,
                    {"samples": candle.sample_count},
                )
            )
        if candle.volume == 0:
            issues.append(
                QualityIssue(
                    "zero_volume",
                    "candle has zero reported volume",
                    IssueSeverity.WARNING,
                    record_id,
                )
            )
    return tuple(issues)
