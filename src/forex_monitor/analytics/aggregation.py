"""Convert quote observations into deterministic OHLCV candles."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Callable, Optional, Sequence, Tuple

from forex_monitor.errors import AnalyticsError
from forex_monitor.models import Candle, Quote, Timeframe, common_symbol
from forex_monitor.timeutils import floor_time, interval_end

PriceSelector = Callable[[Quote], Decimal]


def mid_price(quote: Quote) -> Decimal:
    return quote.mid


def bid_price(quote: Quote) -> Decimal:
    return quote.bid


def ask_price(quote: Quote) -> Decimal:
    return quote.ask


def aggregate_quotes(
    quotes: Sequence[Quote],
    timeframe: Timeframe,
    *,
    price: PriceSelector = mid_price,
    include_empty: bool = False,
    complete_before: Optional[datetime] = None,
) -> Tuple[Candle, ...]:
    """Aggregate a homogeneous quote sequence into ordered candles."""

    if not quotes:
        return ()
    symbol = common_symbol(quotes)
    ordered = sorted(quotes, key=lambda quote: quote.observed_at)
    buckets: dict[datetime, list[Quote]] = {}
    for quote in ordered:
        opened_at = floor_time(quote.observed_at, timeframe)
        buckets.setdefault(opened_at, []).append(quote)
    if include_empty:
        _fill_empty_buckets(buckets, timeframe)
    result = []
    previous_close = None
    for opened_at in sorted(buckets):
        observations = buckets[opened_at]
        closed_at = interval_end(opened_at, timeframe)
        if observations:
            prices = tuple(price(quote) for quote in observations)
            if any(value <= 0 for value in prices):
                raise AnalyticsError("candle prices must be positive")
            volume = sum((quote.volume or Decimal("0") for quote in observations), Decimal("0"))
            candle = Candle(
                symbol=symbol,
                timeframe=timeframe,
                opened_at=opened_at,
                closed_at=closed_at,
                open=prices[0],
                high=max(prices),
                low=min(prices),
                close=prices[-1],
                volume=volume,
                sample_count=len(prices),
                complete=complete_before is None or closed_at <= complete_before,
            )
            previous_close = candle.close
        elif previous_close is not None:
            candle = Candle(
                symbol=symbol,
                timeframe=timeframe,
                opened_at=opened_at,
                closed_at=closed_at,
                open=previous_close,
                high=previous_close,
                low=previous_close,
                close=previous_close,
                volume=Decimal("0"),
                sample_count=1,
                complete=complete_before is None or closed_at <= complete_before,
            )
        else:
            continue
        result.append(candle)
    return tuple(result)


def _fill_empty_buckets(
    buckets: dict[datetime, list[Quote]],
    timeframe: Timeframe,
) -> None:
    if not buckets:
        return
    current = min(buckets)
    final = max(buckets)
    step = timedelta(seconds=timeframe.seconds)
    while current <= final:
        buckets.setdefault(current, [])
        current += step


def resample_candles(
    candles: Sequence[Candle],
    timeframe: Timeframe,
) -> Tuple[Candle, ...]:
    """Aggregate smaller candles into a larger compatible timeframe."""

    if not candles:
        return ()
    source = candles[0].timeframe
    if any(candle.symbol != candles[0].symbol or candle.timeframe != source for candle in candles):
        raise AnalyticsError("candles must share a symbol and source timeframe")
    if timeframe.seconds < source.seconds or timeframe.seconds % source.seconds:
        raise AnalyticsError("target timeframe must be an exact multiple of source timeframe")
    groups: dict[datetime, list[Candle]] = {}
    for candle in sorted(candles, key=lambda item: item.opened_at):
        groups.setdefault(floor_time(candle.opened_at, timeframe), []).append(candle)
    expected_samples = timeframe.seconds // source.seconds
    result = []
    for opened_at in sorted(groups):
        group = groups[opened_at]
        result.append(
            Candle(
                symbol=group[0].symbol,
                timeframe=timeframe,
                opened_at=opened_at,
                closed_at=interval_end(opened_at, timeframe),
                open=group[0].open,
                high=max(candle.high for candle in group),
                low=min(candle.low for candle in group),
                close=group[-1].close,
                volume=sum((candle.volume for candle in group), Decimal("0")),
                sample_count=sum(candle.sample_count for candle in group),
                complete=len(group) == expected_samples
                and all(candle.complete for candle in group),
            )
        )
    return tuple(result)


def validate_candle_continuity(candles: Sequence[Candle]) -> Tuple[datetime, ...]:
    """Return expected opening times missing from a candle sequence."""

    if len(candles) < 2:
        return ()
    ordered = sorted(candles, key=lambda candle: candle.opened_at)
    timeframe = ordered[0].timeframe
    symbol = ordered[0].symbol
    if any(candle.timeframe != timeframe or candle.symbol != symbol for candle in ordered):
        raise AnalyticsError("candles must share a symbol and timeframe")
    missing = []
    step = timedelta(seconds=timeframe.seconds)
    expected = ordered[0].opened_at + step
    for candle in ordered[1:]:
        while expected < candle.opened_at:
            missing.append(expected)
            expected += step
        if candle.opened_at < expected:
            raise AnalyticsError("candle sequence overlaps or contains duplicates")
        expected = candle.opened_at + step
    return tuple(missing)
