"""Deterministic test factories shared by unit and integration suites."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional, Tuple

from forex_monitor.config import DatabaseConfig
from forex_monitor.models import Candle, Quote, Timeframe
from forex_monitor.storage import Database, RepositorySet

UTC = timezone.utc
BASE_TIME = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)


@dataclass
class MutableClock:
    current: datetime = BASE_TIME

    def __call__(self) -> datetime:
        return self.current

    def advance(self, **kwargs: int) -> datetime:
        self.current += timedelta(**kwargs)
        return self.current

    def set(self, value: datetime) -> datetime:
        self.current = value
        return self.current


def quote(
    symbol: str = "EURUSD",
    *,
    bid: object = "1.1000",
    ask: object = "1.1002",
    observed_at: datetime = BASE_TIME,
    provider: str = "fixture",
    source_id: Optional[str] = "source_1",
    volume: Optional[object] = "100",
) -> Quote:
    return Quote(
        symbol=symbol,
        bid=Decimal(str(bid)),
        ask=Decimal(str(ask)),
        observed_at=observed_at,
        provider=provider,
        source_id=source_id,
        volume=Decimal(str(volume)) if volume is not None else None,
    )


def quote_series(
    count: int,
    *,
    symbol: str = "EURUSD",
    start: datetime = BASE_TIME,
    step_seconds: int = 60,
    start_price: object = "1.1000",
    price_step: object = "0.0001",
    provider: str = "fixture",
) -> Tuple[Quote, ...]:
    base = Decimal(str(start_price))
    increment = Decimal(str(price_step))
    result = []
    for index in range(count):
        bid = base + increment * index
        result.append(
            quote(
                symbol,
                bid=bid,
                ask=bid + Decimal("0.0002"),
                observed_at=start + timedelta(seconds=index * step_seconds),
                provider=provider,
                source_id=f"source_{index + 1}",
                volume=100 + index,
            )
        )
    return tuple(result)


def candle(
    index: int = 0,
    *,
    symbol: str = "EURUSD",
    timeframe: Timeframe = Timeframe.HOUR_1,
    start: datetime = BASE_TIME,
    open_price: object = "1.1000",
    change: object = "0.0010",
    volume: object = "1000",
    complete: bool = True,
) -> Candle:
    opened = start + timedelta(seconds=timeframe.seconds * index)
    opening = Decimal(str(open_price)) + Decimal("0.0005") * index
    closing = opening + Decimal(str(change))
    high = max(opening, closing) + Decimal("0.0003")
    low = min(opening, closing) - Decimal("0.0003")
    return Candle(
        symbol=symbol,
        timeframe=timeframe,
        opened_at=opened,
        closed_at=opened + timedelta(seconds=timeframe.seconds),
        open=opening,
        high=high,
        low=low,
        close=closing,
        volume=Decimal(str(volume)),
        sample_count=10,
        complete=complete,
    )


def candle_series(
    count: int,
    *,
    symbol: str = "EURUSD",
    timeframe: Timeframe = Timeframe.HOUR_1,
    alternating: bool = False,
) -> Tuple[Candle, ...]:
    result = []
    for index in range(count):
        direction = Decimal("0.0010")
        if alternating and index % 2:
            direction = Decimal("-0.0007")
        result.append(candle(index, symbol=symbol, timeframe=timeframe, change=direction))
    return tuple(result)


class TemporaryRepositories:
    """Context manager returning initialized repositories on a temporary file."""

    def __init__(self) -> None:
        self._temporary: Optional[tempfile.TemporaryDirectory[str]] = None
        self.database: Optional[Database] = None
        self.repositories: Optional[RepositorySet] = None

    def __enter__(self) -> RepositorySet:
        self._temporary = tempfile.TemporaryDirectory()
        path = Path(self._temporary.name) / "test.db"
        self.database = Database(DatabaseConfig(path=path))
        self.database.initialize()
        self.repositories = RepositorySet(self.database)
        return self.repositories

    def __exit__(self, *args: object) -> None:
        if self._temporary:
            self._temporary.cleanup()
