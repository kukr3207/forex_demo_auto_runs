"""Immutable values shared by regulatory reporting workflows."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Mapping, Optional, Tuple

from forex_monitor.errors import ValidationError


def required(value: object, name: str, maximum: Optional[int] = None) -> str:
    result = str(value).strip() if value is not None else ""
    if not result:
        raise ValidationError("%s is required" % name)
    if maximum is not None and len(result) > maximum:
        raise ValidationError("%s exceeds maximum length" % name)
    return result


def decimal_value(value: object, name: str, *, positive: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError("%s must be numeric" % name) from exc
    if not result.is_finite():
        raise ValidationError("%s must be finite" % name)
    if positive and result <= 0:
        raise ValidationError("%s must be positive" % name)
    return result


def utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError("%s must be timezone-aware" % name)
    return value.astimezone(timezone.utc)


def currency(value: object, name: str = "currency") -> str:
    result = required(value, name).upper()
    if len(result) != 3 or not result.isalpha():
        raise ValidationError("%s must be an ISO currency" % name)
    return result


class AssetClass(str, Enum):
    SPOT = "spot"
    FORWARD = "forward"
    SWAP = "swap"
    NDF = "ndf"
    OPTION = "option"


class ReportAction(str, Enum):
    NEW = "new"
    MODIFY = "modify"
    CANCEL = "cancel"
    CORRECT = "correct"
    TERMINATE = "terminate"


class ReportStatus(str, Enum):
    DRAFT = "draft"
    VALIDATED = "validated"
    SUBMITTED = "submitted"
    ACKNOWLEDGED = "acknowledged"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Party:
    party_id: str
    legal_name: str
    lei: Optional[str]
    country: str
    financial_counterparty: bool
    sector: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "party_id", required(self.party_id, "party id", 64))
        object.__setattr__(self, "legal_name", required(self.legal_name, "legal name", 200))
        if self.lei is not None:
            lei = self.lei.strip().upper()
            if len(lei) != 20 or not lei.isalnum():
                raise ValidationError("LEI must contain twenty alphanumeric characters")
            object.__setattr__(self, "lei", lei)
        country = required(self.country, "country").upper()
        if len(country) != 2 or not country.isalpha():
            raise ValidationError("party country must be ISO alpha-2")
        object.__setattr__(self, "country", country)
        object.__setattr__(self, "sector", self.sector.strip().upper())


@dataclass(frozen=True)
class TradeEconomics:
    trade_id: str
    asset_class: AssetClass
    base_currency: str
    quote_currency: str
    quantity: Decimal
    price: Decimal
    traded_at: datetime
    value_date: date
    maturity_date: Optional[date] = None
    second_quantity: Optional[Decimal] = None
    second_value_date: Optional[date] = None
    venue: Optional[str] = None
    execution_method: str = "electronic"

    def __post_init__(self) -> None:
        object.__setattr__(self, "trade_id", required(self.trade_id, "trade id", 100))
        object.__setattr__(self, "asset_class", AssetClass(self.asset_class))
        base = currency(self.base_currency, "base currency")
        quote = currency(self.quote_currency, "quote currency")
        if base == quote:
            raise ValidationError("trade currencies must differ")
        object.__setattr__(self, "base_currency", base)
        object.__setattr__(self, "quote_currency", quote)
        object.__setattr__(self, "quantity", decimal_value(self.quantity, "quantity", positive=True))
        object.__setattr__(self, "price", decimal_value(self.price, "price", positive=True))
        traded = utc(self.traded_at, "traded at")
        object.__setattr__(self, "traded_at", traded)
        if self.value_date < traded.date():
            raise ValidationError("value date cannot precede trade date")
        if self.maturity_date is not None and self.maturity_date < self.value_date:
            raise ValidationError("maturity cannot precede value date")
        if self.second_quantity is not None:
            object.__setattr__(self, "second_quantity", decimal_value(self.second_quantity, "second quantity", positive=True))
        if self.second_value_date is not None and self.second_value_date <= self.value_date:
            raise ValidationError("second value date must follow first value date")
        if self.asset_class == AssetClass.SWAP and (self.second_quantity is None or self.second_value_date is None):
            raise ValidationError("swap economics require a second leg")
        if self.asset_class != AssetClass.SWAP and (self.second_quantity is not None or self.second_value_date is not None):
            raise ValidationError("second leg is only valid for swaps")
        object.__setattr__(self, "venue", self.venue.strip().upper() if self.venue else None)
        object.__setattr__(self, "execution_method", required(self.execution_method, "execution method").lower())

    @property
    def symbol(self) -> str:
        return self.base_currency + self.quote_currency

    @property
    def quote_notional(self) -> Decimal:
        return self.quantity * self.price


@dataclass(frozen=True)
class TransactionReport:
    report_id: str
    regime: str
    action: ReportAction
    status: ReportStatus
    economics: TradeEconomics
    reporting_party: Party
    counterparty: Party
    unique_transaction_id: str
    generated_at: datetime
    reportable: bool = True
    report_deadline: Optional[datetime] = None
    prior_report_id: Optional[str] = None
    fields: Tuple[Tuple[str, str], ...] = ()
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "report_id", required(self.report_id, "report id", 100))
        object.__setattr__(self, "regime", required(self.regime, "regime", 30).upper())
        object.__setattr__(self, "action", ReportAction(self.action))
        object.__setattr__(self, "status", ReportStatus(self.status))
        object.__setattr__(self, "unique_transaction_id", required(self.unique_transaction_id, "UTI", 100))
        object.__setattr__(self, "generated_at", utc(self.generated_at, "generated at"))
        if self.report_deadline is not None:
            deadline = utc(self.report_deadline, "report deadline")
            if deadline < self.economics.traded_at:
                raise ValidationError("report deadline cannot precede trade")
            object.__setattr__(self, "report_deadline", deadline)
        if self.action != ReportAction.NEW and not self.prior_report_id:
            raise ValidationError("lifecycle report requires a prior report id")
        if self.version < 1:
            raise ValidationError("report version must be positive")
        names = [key for key, _ in self.fields]
        if len(set(names)) != len(names):
            raise ValidationError("report fields contain duplicate names")
        object.__setattr__(self, "fields", tuple(sorted((required(k, "field name"), str(v)) for k, v in self.fields)))

    def as_dict(self) -> Mapping[str, object]:
        result = {
            "reportId": self.report_id,
            "regime": self.regime,
            "action": self.action.value,
            "status": self.status.value,
            "tradeId": self.economics.trade_id,
            "uti": self.unique_transaction_id,
            "symbol": self.economics.symbol,
            "quantity": str(self.economics.quantity),
            "price": str(self.economics.price),
            "tradedAt": self.economics.traded_at.isoformat(),
            "valueDate": self.economics.value_date.isoformat(),
            "reportingPartyLei": self.reporting_party.lei,
            "counterpartyLei": self.counterparty.lei,
            "reportable": self.reportable,
            "version": self.version,
        }
        result.update(dict(self.fields))
        return result


@dataclass(frozen=True)
class ValidationFinding:
    field: str
    code: str
    message: str
    severity: str = "error"

    def __post_init__(self) -> None:
        if self.severity not in {"error", "warning"}:
            raise ValidationError("finding severity is invalid")


@dataclass(frozen=True)
class SubmissionReceipt:
    submission_id: str
    report_id: str
    destination: str
    submitted_at: datetime
    payload_hash: str
    accepted: Optional[bool] = None
    response_code: Optional[str] = None
    response_message: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "submission_id", required(self.submission_id, "submission id"))
        object.__setattr__(self, "report_id", required(self.report_id, "report id"))
        object.__setattr__(self, "destination", required(self.destination, "destination").upper())
        object.__setattr__(self, "submitted_at", utc(self.submitted_at, "submitted at"))
        digest = required(self.payload_hash, "payload hash").lower()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValidationError("payload hash must be SHA-256")
        object.__setattr__(self, "payload_hash", digest)
