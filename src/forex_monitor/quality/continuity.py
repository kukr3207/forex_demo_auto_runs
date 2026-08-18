"""Detect missing intervals in ordered candle series."""

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence, Tuple

from forex_monitor.models import Candle, ensure_utc


@dataclass(frozen=True)
class MissingInterval:
    expected_at: datetime
    resumed_at: datetime
    missing_buckets: int


def missing_intervals(candles: Sequence[Candle]) -> Tuple[MissingInterval, ...]:
    if len(candles) < 2:
        return ()
    ordered = sorted(candles, key=lambda candle: candle.opened_at)
    duration = ordered[0].closed_at - ordered[0].opened_at
    if duration.total_seconds() <= 0:
        raise ValueError("candle duration must be positive")
    result = []
    for previous, current in zip(ordered, ordered[1:]):
        expected = previous.opened_at + duration
        if current.opened_at > expected:
            buckets = int((current.opened_at - expected) / duration)
            result.append(MissingInterval(ensure_utc(expected), current.opened_at, buckets))
    return tuple(result)
