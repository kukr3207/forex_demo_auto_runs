"""Tax domain records for FX positions and client payments."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Optional, Tuple

from forex_monitor.errors import ValidationError


def text(value: object, name: str, maximum: Optional[int] = None) -> str:
    result = str(value).strip() if value is not None else ""
    if not result:
        raise ValidationError("%s is required" % name)
    if maximum is not None and len(result) > maximum:
        raise ValidationError("%s exceeds maximum length" % name)
    return result


def number(value: object, name: str, *, positive: bool = False, non_negative: bool = False) -> Decimal:
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


def utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError("%s must be timezone-aware" % name)
    return value.astimezone(timezone.utc)


def currency(value: object) -> str:
    result = text(value, "currency").upper()
    if len(result) != 3 or not result.isalpha():
        raise ValidationError("currency is invalid")
    return result


class LotMethod(str, Enum):
    FIFO = "fifo"
    LIFO = "lifo"
    HIFO = "hifo"
    SPECIFIC = "specific"


class IncomeType(str, Enum):
    INTEREST = "interest"
    REBATE = "rebate"
    COMMISSION = "commission"
    SERVICE_FEE = "service_fee"
    OTHER = "other"


@dataclass(frozen=True)
class TaxProfile:
    profile_id: str
    client_id: str
    tax_country: str
    tax_identifier: str
    entity_type: str
    treaty_country: Optional[str] = None
    certified_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    exempt: bool = False
    backup_withholding: bool = False
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "profile_id", text(self.profile_id, "tax profile id"))
        object.__setattr__(self, "client_id", text(self.client_id, "client id"))
        country = text(self.tax_country, "tax country").upper()
        if len(country) != 2:
            raise ValidationError("tax country must be ISO alpha-2")
        object.__setattr__(self, "tax_country", country)
        object.__setattr__(self, "tax_identifier", text(self.tax_identifier, "tax identifier"))
        object.__setattr__(self, "entity_type", text(self.entity_type, "entity type").lower())
        if self.treaty_country is not None:
            treaty = self.treaty_country.upper()
            if len(treaty) != 2:
                raise ValidationError("treaty country must be ISO alpha-2")
            object.__setattr__(self, "treaty_country", treaty)
        if self.certified_at is not None:
            object.__setattr__(self, "certified_at", utc(self.certified_at, "certified at"))
        if self.expires_at is not None:
            expiry = utc(self.expires_at, "expires at")
            if self.certified_at and expiry <= self.certified_at:
                raise ValidationError("tax certification expiry is invalid")
            object.__setattr__(self, "expires_at", expiry)


@dataclass(frozen=True)
class TaxLot:
    lot_id: str
    account_id: str
    instrument_id: str
    currency: str
    acquired_at: datetime
    original_quantity: Decimal
    remaining_quantity: Decimal
    unit_cost: Decimal
    fees: Decimal = Decimal("0")
    source_reference: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "lot_id", text(self.lot_id, "lot id"))
        object.__setattr__(self, "account_id", text(self.account_id, "account id"))
        object.__setattr__(self, "instrument_id", text(self.instrument_id, "instrument id"))
        object.__setattr__(self, "currency", currency(self.currency))
        object.__setattr__(self, "acquired_at", utc(self.acquired_at, "acquired at"))
        original = number(self.original_quantity, "original quantity", positive=True)
        remaining = number(self.remaining_quantity, "remaining quantity", non_negative=True)
        if remaining > original:
            raise ValidationError("remaining lot quantity exceeds original")
        object.__setattr__(self, "original_quantity", original)
        object.__setattr__(self, "remaining_quantity", remaining)
        object.__setattr__(self, "unit_cost", number(self.unit_cost, "unit cost", non_negative=True))
        object.__setattr__(self, "fees", number(self.fees, "fees", non_negative=True))
        object.__setattr__(self, "source_reference", text(self.source_reference, "source reference"))

    @property
    def remaining_cost_basis(self) -> Decimal:
        allocated_fees = self.fees * self.remaining_quantity / self.original_quantity
        return self.remaining_quantity * self.unit_cost + allocated_fees


@dataclass(frozen=True)
class LotDisposal:
    disposal_id: str
    lot_id: str
    account_id: str
    instrument_id: str
    quantity: Decimal
    proceeds: Decimal
    cost_basis: Decimal
    disposed_at: datetime
    acquired_at: datetime
    source_reference: str

    @property
    def gain(self) -> Decimal:
        return self.proceeds - self.cost_basis

    @property
    def holding_days(self) -> int:
        return (self.disposed_at.date() - self.acquired_at.date()).days

    @property
    def long_term(self) -> bool:
        return self.holding_days > 365


@dataclass(frozen=True)
class WithholdingRule:
    rule_id: str
    source_country: str
    income_type: IncomeType
    default_rate: Decimal
    treaty_rates: Tuple[Tuple[str, Decimal], ...] = ()
    entity_rates: Tuple[Tuple[str, Decimal], ...] = ()
    effective_from: date = date.min
    effective_until: Optional[date] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "rule_id", text(self.rule_id, "withholding rule id"))
        country = text(self.source_country, "source country").upper()
        if len(country) != 2:
            raise ValidationError("source country is invalid")
        object.__setattr__(self, "source_country", country)
        object.__setattr__(self, "income_type", IncomeType(self.income_type))
        rate = number(self.default_rate, "default rate", non_negative=True)
        if rate > 1:
            raise ValidationError("default rate cannot exceed one")
        object.__setattr__(self, "default_rate", rate)
        treaty = tuple(sorted((country.upper(), number(value, "treaty rate", non_negative=True)) for country, value in self.treaty_rates))
        entity = tuple(sorted((kind.lower(), number(value, "entity rate", non_negative=True)) for kind, value in self.entity_rates))
        if any(value > 1 for _, value in treaty + entity):
            raise ValidationError("withholding rate cannot exceed one")
        object.__setattr__(self, "treaty_rates", treaty)
        object.__setattr__(self, "entity_rates", entity)
        if self.effective_until and self.effective_until < self.effective_from:
            raise ValidationError("withholding rule validity is invalid")

    def effective_on(self, day: date) -> bool:
        return self.effective_from <= day and (self.effective_until is None or day <= self.effective_until)


@dataclass(frozen=True)
class WithholdingResult:
    calculation_id: str
    client_id: str
    income_type: IncomeType
    gross_amount: Decimal
    rate: Decimal
    withheld_amount: Decimal
    net_amount: Decimal
    currency: str
    calculated_at: datetime
    rule_id: str
    reason: str


@dataclass(frozen=True)
class TaxStatement:
    statement_id: str
    client_id: str
    tax_year: int
    currency: str
    short_term_gain: Decimal
    long_term_gain: Decimal
    income: Decimal
    withheld: Decimal
    generated_at: datetime
    transaction_count: int
    correction_of: Optional[str] = None
    version: int = 1
