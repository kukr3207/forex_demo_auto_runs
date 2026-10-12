"""Validated reference-data records and temporal versions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from forex_monitor.errors import ValidationError


def required(value: object, name: str, maximum: Optional[int] = None) -> str:
    result = str(value).strip() if value is not None else ""
    if not result:
        raise ValidationError("%s is required" % name)
    if maximum is not None and len(result) > maximum:
        raise ValidationError("%s exceeds maximum length" % name)
    return result


def decimal_value(value: object, name: str, *, positive: bool = False, non_negative: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError("%s must be numeric" % name) from exc
    if not result.is_finite():
        raise ValidationError("%s must be finite" % name)
    if positive and result <= 0:
        raise ValidationError("%s must be positive" % name)
    if non_negative and result < 0:
        raise ValidationError("%s cannot be negative" % name)
    return result


def currency_code(value: object) -> str:
    result = required(value, "currency code").upper()
    if len(result) != 3 or not result.isalpha():
        raise ValidationError("currency code must contain three letters")
    return result


def utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError("%s must be timezone-aware" % name)
    return value.astimezone(timezone.utc)


class InstrumentType(str, Enum):
    SPOT = "spot"
    FORWARD = "forward"
    SWAP = "swap"
    NDF = "ndf"
    OPTION = "option"


class IdentifierType(str, Enum):
    INTERNAL = "internal"
    RIC = "ric"
    BLOOMBERG = "bloomberg"
    ISIN = "isin"
    FIGI = "figi"
    UPI = "upi"


class DayRoll(str, Enum):
    FOLLOWING = "following"
    MODIFIED_FOLLOWING = "modified_following"
    PRECEDING = "preceding"
    MODIFIED_PRECEDING = "modified_preceding"
    UNADJUSTED = "unadjusted"


@dataclass(frozen=True)
class CurrencyDefinition:
    code: str
    numeric_code: str
    name: str
    minor_units: int
    active: bool = True
    country_codes: Tuple[str, ...] = ()
    settlement_calendar: Optional[str] = None
    restricted: bool = False
    deliverable: bool = True
    effective_from: date = date.min
    effective_until: Optional[date] = None
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", currency_code(self.code))
        numeric = required(self.numeric_code, "numeric currency code")
        if len(numeric) != 3 or not numeric.isdigit():
            raise ValidationError("numeric currency code must contain three digits")
        object.__setattr__(self, "numeric_code", numeric)
        object.__setattr__(self, "name", required(self.name, "currency name", 100))
        if self.minor_units < 0 or self.minor_units > 6:
            raise ValidationError("currency minor units are invalid")
        countries = tuple(sorted(set(item.upper() for item in self.country_codes)))
        if any(len(item) != 2 or not item.isalpha() for item in countries):
            raise ValidationError("currency countries must use ISO alpha-2")
        object.__setattr__(self, "country_codes", countries)
        object.__setattr__(self, "settlement_calendar", self.settlement_calendar.strip().upper() if self.settlement_calendar else None)
        if self.effective_until is not None and self.effective_until < self.effective_from:
            raise ValidationError("currency end date precedes effective date")
        if self.version < 1:
            raise ValidationError("currency version must be positive")

    def effective_on(self, value: date) -> bool:
        return self.effective_from <= value and (self.effective_until is None or value <= self.effective_until)


@dataclass(frozen=True)
class MarketSession:
    name: str
    timezone_name: str
    opens_at: time
    closes_at: time
    weekdays: Tuple[int, ...] = (0, 1, 2, 3, 4)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", required(self.name, "session name"))
        try:
            ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValidationError("market session timezone is unknown") from exc
        if self.opens_at == self.closes_at:
            raise ValidationError("market session cannot have zero duration")
        weekdays = tuple(sorted(set(self.weekdays)))
        if not weekdays or any(day < 0 or day > 6 for day in weekdays):
            raise ValidationError("market session weekdays are invalid")
        object.__setattr__(self, "weekdays", weekdays)


@dataclass(frozen=True)
class Holiday:
    day: date
    name: str
    early_close: Optional[time] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", required(self.name, "holiday name"))


@dataclass(frozen=True)
class HolidayCalendar:
    calendar_id: str
    name: str
    timezone_name: str
    holidays: Tuple[Holiday, ...]
    weekend_days: Tuple[int, ...] = (5, 6)
    valid_from: date = date.min
    valid_until: Optional[date] = None
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "calendar_id", required(self.calendar_id, "calendar id").upper())
        object.__setattr__(self, "name", required(self.name, "calendar name"))
        try:
            ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValidationError("calendar timezone is unknown") from exc
        days = [item.day for item in self.holidays]
        if len(days) != len(set(days)):
            raise ValidationError("calendar contains duplicate holiday dates")
        object.__setattr__(self, "holidays", tuple(sorted(self.holidays, key=lambda item: item.day)))
        weekends = tuple(sorted(set(self.weekend_days)))
        if any(day < 0 or day > 6 for day in weekends):
            raise ValidationError("calendar weekend days are invalid")
        object.__setattr__(self, "weekend_days", weekends)
        if self.valid_until is not None and self.valid_until < self.valid_from:
            raise ValidationError("calendar validity is invalid")


@dataclass(frozen=True)
class FxInstrument:
    instrument_id: str
    symbol: str
    instrument_type: InstrumentType
    base_currency: str
    quote_currency: str
    price_precision: int
    quantity_precision: int
    minimum_quantity: Decimal
    quantity_increment: Decimal
    spot_lag: int
    calendars: Tuple[str, ...]
    day_roll: DayRoll = DayRoll.MODIFIED_FOLLOWING
    fixing_source: Optional[str] = None
    settlement_currency: Optional[str] = None
    active: bool = True
    effective_from: date = date.min
    effective_until: Optional[date] = None
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "instrument_id", required(self.instrument_id, "instrument id"))
        base = currency_code(self.base_currency)
        quote = currency_code(self.quote_currency)
        if base == quote:
            raise ValidationError("instrument currencies must differ")
        symbol = required(self.symbol, "symbol").upper().replace("/", "")
        if symbol != base + quote:
            raise ValidationError("instrument symbol must match its currencies")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "instrument_type", InstrumentType(self.instrument_type))
        object.__setattr__(self, "base_currency", base)
        object.__setattr__(self, "quote_currency", quote)
        if self.price_precision < 0 or self.price_precision > 12:
            raise ValidationError("price precision is invalid")
        if self.quantity_precision < 0 or self.quantity_precision > 12:
            raise ValidationError("quantity precision is invalid")
        object.__setattr__(self, "minimum_quantity", decimal_value(self.minimum_quantity, "minimum quantity", positive=True))
        increment = decimal_value(self.quantity_increment, "quantity increment", positive=True)
        if increment > self.minimum_quantity:
            raise ValidationError("quantity increment exceeds minimum quantity")
        object.__setattr__(self, "quantity_increment", increment)
        if self.spot_lag < 0 or self.spot_lag > 10:
            raise ValidationError("spot lag is invalid")
        calendars = tuple(sorted(set(item.strip().upper() for item in self.calendars if item.strip())))
        if not calendars:
            raise ValidationError("instrument requires a settlement calendar")
        object.__setattr__(self, "calendars", calendars)
        object.__setattr__(self, "day_roll", DayRoll(self.day_roll))
        object.__setattr__(self, "fixing_source", self.fixing_source.strip().upper() if self.fixing_source else None)
        if self.settlement_currency is not None:
            object.__setattr__(self, "settlement_currency", currency_code(self.settlement_currency))
        if self.instrument_type == InstrumentType.NDF and not self.fixing_source:
            raise ValidationError("NDF instrument requires a fixing source")
        if self.instrument_type == InstrumentType.NDF and not self.settlement_currency:
            raise ValidationError("NDF instrument requires a settlement currency")
        if self.effective_until is not None and self.effective_until < self.effective_from:
            raise ValidationError("instrument validity is invalid")

    def effective_on(self, value: date) -> bool:
        return self.effective_from <= value and (self.effective_until is None or value <= self.effective_until)

    def valid_quantity(self, value: object) -> bool:
        quantity = decimal_value(value, "quantity", positive=True)
        if quantity < self.minimum_quantity:
            return False
        return quantity % self.quantity_increment == 0


@dataclass(frozen=True)
class IdentifierAssignment:
    identifier_type: IdentifierType
    value: str
    entity_type: str
    entity_id: str
    valid_from: date
    valid_until: Optional[date] = None
    primary: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "identifier_type", IdentifierType(self.identifier_type))
        value = required(self.value, "identifier value", 100).upper()
        if self.identifier_type == IdentifierType.ISIN and (len(value) != 12 or not value.isalnum()):
            raise ValidationError("ISIN must contain twelve alphanumeric characters")
        if self.identifier_type == IdentifierType.FIGI and (len(value) != 12 or not value.isalnum()):
            raise ValidationError("FIGI must contain twelve alphanumeric characters")
        object.__setattr__(self, "value", value)
        object.__setattr__(self, "entity_type", required(self.entity_type, "entity type").lower())
        object.__setattr__(self, "entity_id", required(self.entity_id, "entity id"))
        if self.valid_until is not None and self.valid_until < self.valid_from:
            raise ValidationError("identifier validity is invalid")

    def valid_on(self, value: date) -> bool:
        return self.valid_from <= value and (self.valid_until is None or value <= self.valid_until)
