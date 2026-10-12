"""Day-count conventions used by money-market and FX products."""

from __future__ import annotations

from calendar import isleap
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Iterable

from forex_monitor.errors import ValidationError


class DayCount(str, Enum):
    ACT_360 = "ACT/360"
    ACT_365_FIXED = "ACT/365F"
    ACT_ACT = "ACT/ACT"
    THIRTY_360_US = "30/360 US"
    THIRTY_E_360 = "30E/360"

    @classmethod
    def parse(cls, value: object) -> "DayCount":
        if isinstance(value, cls):
            return value
        if not isinstance(value, str):
            raise ValidationError("day count must be a string")
        normalized = value.strip().upper().replace(" ", "")
        aliases = {
            "ACT/360": cls.ACT_360,
            "ACT360": cls.ACT_360,
            "ACT/365F": cls.ACT_365_FIXED,
            "ACT365F": cls.ACT_365_FIXED,
            "ACT/ACT": cls.ACT_ACT,
            "ACTACT": cls.ACT_ACT,
            "30/360US": cls.THIRTY_360_US,
            "30U/360": cls.THIRTY_360_US,
            "30E/360": cls.THIRTY_E_360,
        }
        try:
            return aliases[normalized]
        except KeyError as exc:
            raise ValidationError("day count is not supported") from exc


def _validate_dates(start: date, end: date) -> None:
    if not isinstance(start, date) or not isinstance(end, date):
        raise ValidationError("day-count boundaries must be dates")
    if end < start:
        raise ValidationError("day-count end cannot precede start")


def actual_days(start: date, end: date) -> int:
    _validate_dates(start, end)
    return (end - start).days


def _act_act_segments(start: date, end: date) -> Iterable[Decimal]:
    cursor = start
    while cursor < end:
        next_year = date(cursor.year + 1, 1, 1)
        boundary = min(next_year, end)
        denominator = Decimal(366 if isleap(cursor.year) else 365)
        yield Decimal((boundary - cursor).days) / denominator
        cursor = boundary


def year_fraction(start: date, end: date, convention: object) -> Decimal:
    """Calculate an accrual fraction with explicit end-exclusive semantics."""

    _validate_dates(start, end)
    parsed = DayCount.parse(convention)
    if start == end:
        return Decimal("0")
    if parsed is DayCount.ACT_360:
        return Decimal((end - start).days) / Decimal(360)
    if parsed is DayCount.ACT_365_FIXED:
        return Decimal((end - start).days) / Decimal(365)
    if parsed is DayCount.ACT_ACT:
        return sum(_act_act_segments(start, end), Decimal("0"))
    start_day = start.day
    end_day = end.day
    if parsed is DayCount.THIRTY_E_360:
        start_day = min(start_day, 30)
        end_day = min(end_day, 30)
    else:
        if start_day == 31:
            start_day = 30
        if end_day == 31 and start_day >= 30:
            end_day = 30
    days = (
        (end.year - start.year) * 360
        + (end.month - start.month) * 30
        + end_day
        - start_day
    )
    return Decimal(days) / Decimal(360)


def compounded_factor(rate: Decimal, fraction: Decimal, periods: int = 1) -> Decimal:
    if periods <= 0:
        raise ValidationError("compounding periods must be positive")
    if fraction < 0:
        raise ValidationError("accrual fraction cannot be negative")
    base = Decimal("1") + rate / Decimal(periods)
    if base <= 0:
        raise ValidationError("rate produces a non-positive compounding base")
    exponent = fraction * Decimal(periods)
    return Decimal(str(float(base) ** float(exponent)))
