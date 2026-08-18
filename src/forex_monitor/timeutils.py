"""UTC interval and market-session helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable, Iterator, Optional, Sequence, Tuple

from forex_monitor.models import Timeframe, ensure_utc


UTC = timezone.utc


def floor_time(value: datetime, timeframe: Timeframe) -> datetime:
    """Floor an instant to a timeframe boundary measured from the Unix epoch."""

    instant = ensure_utc(value)
    seconds = Timeframe.parse(timeframe).seconds
    epoch_seconds = int(instant.timestamp())
    floored = epoch_seconds - (epoch_seconds % seconds)
    return datetime.fromtimestamp(floored, tz=UTC)


def ceil_time(value: datetime, timeframe: Timeframe) -> datetime:
    """Return the first timeframe boundary at or after an instant."""

    instant = ensure_utc(value)
    floor = floor_time(instant, timeframe)
    return floor if floor == instant else floor + timedelta(seconds=timeframe.seconds)


def interval_end(opened_at: datetime, timeframe: Timeframe) -> datetime:
    """Return the exclusive end of the bucket containing opened_at."""

    return floor_time(opened_at, timeframe) + timedelta(seconds=timeframe.seconds)


def next_utc_midnight(value: datetime) -> datetime:
    instant = ensure_utc(value)
    return datetime.combine(instant.date() + timedelta(days=1), time.min, tzinfo=UTC)


def utc_day_bounds(value: datetime) -> Tuple[datetime, datetime]:
    instant = ensure_utc(value)
    start = datetime.combine(instant.date(), time.min, tzinfo=UTC)
    return start, start + timedelta(days=1)


def utc_week_bounds(value: datetime) -> Tuple[datetime, datetime]:
    instant = ensure_utc(value)
    start_date = instant.date() - timedelta(days=instant.weekday())
    start = datetime.combine(start_date, time.min, tzinfo=UTC)
    return start, start + timedelta(days=7)


def parse_duration(value: str) -> timedelta:
    """Parse a compact positive duration such as 30s, 5m, 2h, or 7d."""

    if not isinstance(value, str) or len(value.strip()) < 2:
        raise ValueError("duration must include a number and unit")
    text = value.strip().lower()
    unit = text[-1]
    try:
        amount = int(text[:-1])
    except ValueError as error:
        raise ValueError("duration amount must be an integer") from error
    if amount < 1:
        raise ValueError("duration must be positive")
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86_400, "w": 604_800}
    if unit not in multipliers:
        raise ValueError("duration unit must be s, m, h, d, or w")
    seconds = amount * multipliers[unit]
    if seconds > 10 * 365 * 86_400:
        raise ValueError("duration cannot exceed ten years")
    return timedelta(seconds=seconds)


def format_duration(value: timedelta) -> str:
    """Return the largest exact compact duration representation."""

    seconds = int(value.total_seconds())
    if seconds < 0 or value.microseconds:
        raise ValueError("duration must be a non-negative whole number of seconds")
    for unit, size in (("w", 604_800), ("d", 86_400), ("h", 3600), ("m", 60)):
        if seconds and seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def iter_buckets(
    start: datetime,
    end: datetime,
    timeframe: Timeframe,
) -> Iterator[Tuple[datetime, datetime]]:
    """Yield half-open timeframe buckets overlapping [start, end)."""

    first = floor_time(start, timeframe)
    final = ensure_utc(end)
    if final <= ensure_utc(start):
        raise ValueError("end must be later than start")
    current = first
    step = timedelta(seconds=timeframe.seconds)
    while current < final:
        yield current, current + step
        current += step


def is_forex_market_open(value: datetime) -> bool:
    """Return the conventional global FX weekly session state in UTC.

    The market opens Sunday at 22:00 UTC and closes Friday at 22:00 UTC.
    Holiday calendars are intentionally handled separately.
    """

    instant = ensure_utc(value)
    weekday = instant.weekday()
    if weekday == 5:
        return False
    if weekday == 6:
        return instant.time() >= time(22, 0)
    if weekday == 4:
        return instant.time() < time(22, 0)
    return True


def next_market_open(value: datetime) -> datetime:
    """Return value when open, otherwise the next Sunday 22:00 UTC."""

    instant = ensure_utc(value)
    if is_forex_market_open(instant):
        return instant
    days_until_sunday = (6 - instant.weekday()) % 7
    sunday = instant.date() + timedelta(days=days_until_sunday)
    candidate = datetime.combine(sunday, time(22, 0), tzinfo=UTC)
    if candidate < instant:
        candidate += timedelta(days=7)
    return candidate


@dataclass(frozen=True)
class TimeWindow:
    """Validated half-open UTC interval."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        start = ensure_utc(self.start, "window start")
        end = ensure_utc(self.end, "window end")
        if end <= start:
            raise ValueError("window end must be later than start")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    def contains(self, value: datetime) -> bool:
        instant = ensure_utc(value)
        return self.start <= instant < self.end

    def overlaps(self, other: "TimeWindow") -> bool:
        return self.start < other.end and other.start < self.end

    def intersection(self, other: "TimeWindow") -> Optional["TimeWindow"]:
        start = max(self.start, other.start)
        end = min(self.end, other.end)
        return TimeWindow(start, end) if start < end else None

    def split(self, timeframe: Timeframe) -> Tuple["TimeWindow", ...]:
        return tuple(TimeWindow(start, end) for start, end in iter_buckets(self.start, self.end, timeframe))


def merge_windows(windows: Iterable[TimeWindow]) -> Tuple[TimeWindow, ...]:
    """Merge overlapping or touching windows in chronological order."""

    ordered = sorted(windows, key=lambda window: (window.start, window.end))
    if not ordered:
        return ()
    merged = [ordered[0]]
    for window in ordered[1:]:
        previous = merged[-1]
        if window.start <= previous.end:
            merged[-1] = TimeWindow(previous.start, max(previous.end, window.end))
        else:
            merged.append(window)
    return tuple(merged)


def missing_windows(
    requested: TimeWindow,
    available: Sequence[TimeWindow],
) -> Tuple[TimeWindow, ...]:
    """Subtract available coverage from a requested interval."""

    intersections = []
    for window in merge_windows(available):
        intersection = requested.intersection(window)
        if intersection:
            intersections.append(intersection)
    if not intersections:
        return (requested,)
    result = []
    cursor = requested.start
    for window in intersections:
        if cursor < window.start:
            result.append(TimeWindow(cursor, window.start))
        cursor = max(cursor, window.end)
    if cursor < requested.end:
        result.append(TimeWindow(cursor, requested.end))
    return tuple(result)

