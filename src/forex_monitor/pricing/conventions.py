"""Currency, calendar, settlement, and quotation conventions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import Enum
from typing import Dict, Iterable, Mapping, Optional, Sequence, Tuple

from forex_monitor.errors import ValidationError
from forex_monitor.models import as_decimal, normalize_symbol
from forex_monitor.pricing.day_count import DayCount


class BusinessDayRule(str, Enum):
    FOLLOWING = "following"
    MODIFIED_FOLLOWING = "modified_following"
    PRECEDING = "preceding"
    MODIFIED_PRECEDING = "modified_preceding"


@dataclass(frozen=True)
class HolidayCalendar:
    name: str
    holidays: Tuple[date, ...] = ()
    weekend: Tuple[int, ...] = (5, 6)

    def __post_init__(self) -> None:
        name = self.name.strip()
        if not name:
            raise ValidationError("calendar name cannot be blank")
        if any(day < 0 or day > 6 for day in self.weekend):
            raise ValidationError("weekend days must be between zero and six")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "holidays", tuple(sorted(set(self.holidays))))
        object.__setattr__(self, "weekend", tuple(sorted(set(self.weekend))))

    def is_business_day(self, value: date) -> bool:
        return value.weekday() not in self.weekend and value not in self.holidays

    def shift(self, value: date, business_days: int) -> date:
        if business_days == 0:
            return value
        direction = 1 if business_days > 0 else -1
        remaining = abs(business_days)
        cursor = value
        while remaining:
            cursor += timedelta(days=direction)
            if self.is_business_day(cursor):
                remaining -= 1
        return cursor

    def adjust(self, value: date, rule: BusinessDayRule) -> date:
        if self.is_business_day(value):
            return value
        direction = -1 if rule in {
            BusinessDayRule.PRECEDING,
            BusinessDayRule.MODIFIED_PRECEDING,
        } else 1
        cursor = value
        while not self.is_business_day(cursor):
            cursor += timedelta(days=direction)
        modified = rule in {
            BusinessDayRule.MODIFIED_FOLLOWING,
            BusinessDayRule.MODIFIED_PRECEDING,
        }
        if modified and cursor.month != value.month:
            opposite = -direction
            cursor = value
            while not self.is_business_day(cursor):
                cursor += timedelta(days=opposite)
        return cursor


@dataclass(frozen=True)
class CurrencyConvention:
    code: str
    day_count: DayCount = DayCount.ACT_360
    settlement_days: int = 2
    amount_precision: int = 2
    default_calendar: str = "WEEKDAY"

    def __post_init__(self) -> None:
        code = self.code.strip().upper()
        if len(code) != 3 or not code.isalpha():
            raise ValidationError("currency code must contain three letters")
        if self.settlement_days < 0 or self.settlement_days > 10:
            raise ValidationError("settlement days must be between zero and ten")
        if self.amount_precision < 0 or self.amount_precision > 8:
            raise ValidationError("amount precision must be between zero and eight")
        object.__setattr__(self, "code", code)
        object.__setattr__(self, "day_count", DayCount.parse(self.day_count))
        object.__setattr__(self, "default_calendar", self.default_calendar.strip().upper())


@dataclass(frozen=True)
class PairConvention:
    symbol: str
    base_currency: str
    quote_currency: str
    pip_size: Decimal
    spot_lag: int = 2
    inverse_quotation: bool = False

    def __post_init__(self) -> None:
        symbol = normalize_symbol(self.symbol)
        base = self.base_currency.strip().upper()
        quote = self.quote_currency.strip().upper()
        if symbol != base + quote:
            raise ValidationError("pair symbol must match its currencies")
        if base == quote or len(base) != 3 or len(quote) != 3:
            raise ValidationError("pair currencies must be distinct three-letter codes")
        if self.spot_lag < 0 or self.spot_lag > 10:
            raise ValidationError("spot lag must be between zero and ten")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "base_currency", base)
        object.__setattr__(self, "quote_currency", quote)
        object.__setattr__(self, "pip_size", as_decimal(self.pip_size, "pip size", positive=True))

    def points_to_rate(self, points: object) -> Decimal:
        return as_decimal(points, "forward points") * self.pip_size

    def rate_to_points(self, rate_difference: object) -> Decimal:
        return as_decimal(rate_difference, "rate difference") / self.pip_size


class MarketConventions:
    """Validated registry for calendars, currencies, and pair quoting rules."""

    def __init__(self) -> None:
        self._calendars: Dict[str, HolidayCalendar] = {
            "WEEKDAY": HolidayCalendar("WEEKDAY")
        }
        self._currencies: Dict[str, CurrencyConvention] = {}
        self._pairs: Dict[str, PairConvention] = {}

    def add_calendar(self, calendar: HolidayCalendar) -> None:
        if calendar.name in self._calendars:
            raise ValidationError("calendar is already registered")
        self._calendars[calendar.name] = calendar

    def add_currency(self, convention: CurrencyConvention) -> None:
        if convention.default_calendar not in self._calendars:
            raise ValidationError("currency references an unknown calendar")
        if convention.code in self._currencies:
            raise ValidationError("currency convention is already registered")
        self._currencies[convention.code] = convention

    def add_pair(self, convention: PairConvention) -> None:
        for code in (convention.base_currency, convention.quote_currency):
            if code not in self._currencies:
                raise ValidationError("pair references an unknown currency")
        if convention.symbol in self._pairs:
            raise ValidationError("pair convention is already registered")
        self._pairs[convention.symbol] = convention

    def currency(self, code: str) -> CurrencyConvention:
        normalized = code.strip().upper()
        try:
            return self._currencies[normalized]
        except KeyError as exc:
            raise ValidationError("currency convention is not registered") from exc

    def pair(self, symbol: str) -> PairConvention:
        normalized = normalize_symbol(symbol)
        try:
            return self._pairs[normalized]
        except KeyError as exc:
            raise ValidationError("pair convention is not registered") from exc

    def calendar(self, name: str) -> HolidayCalendar:
        normalized = name.strip().upper()
        try:
            return self._calendars[normalized]
        except KeyError as exc:
            raise ValidationError("holiday calendar is not registered") from exc

    def spot_date(self, symbol: str, trade_date: date) -> date:
        pair = self.pair(symbol)
        base = self.currency(pair.base_currency)
        quote = self.currency(pair.quote_currency)
        calendars = (
            self.calendar(base.default_calendar),
            self.calendar(quote.default_calendar),
        )
        cursor = trade_date
        remaining = pair.spot_lag
        while remaining:
            cursor += timedelta(days=1)
            if all(calendar.is_business_day(cursor) for calendar in calendars):
                remaining -= 1
        return cursor

    @classmethod
    def standard(cls) -> "MarketConventions":
        registry = cls()
        defaults = {
            "USD": DayCount.ACT_360,
            "EUR": DayCount.ACT_360,
            "GBP": DayCount.ACT_365_FIXED,
            "JPY": DayCount.ACT_360,
            "CHF": DayCount.ACT_360,
            "CAD": DayCount.ACT_365_FIXED,
            "AUD": DayCount.ACT_365_FIXED,
            "NZD": DayCount.ACT_365_FIXED,
        }
        for code, basis in defaults.items():
            registry.add_currency(CurrencyConvention(code, basis, 2, 0 if code == "JPY" else 2))
        for symbol in ("EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD"):
            registry.add_pair(
                PairConvention(
                    symbol,
                    symbol[:3],
                    symbol[3:],
                    Decimal("0.01") if symbol.endswith("JPY") else Decimal("0.0001"),
                )
            )
        return registry

    def as_dict(self) -> Mapping[str, object]:
        return {
            "currencies": tuple(sorted(self._currencies)),
            "pairs": tuple(sorted(self._pairs)),
            "calendars": tuple(sorted(self._calendars)),
        }
