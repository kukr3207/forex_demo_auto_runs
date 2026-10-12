"""Custody accounts, cash movements, reservations, and transfers."""

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


def money(value: object, name: str, *, positive: bool = False, non_negative: bool = False) -> Decimal:
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


class AccountStatus(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    RESTRICTED = "restricted"
    CLOSED = "closed"


class MovementType(str, Enum):
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"
    TRANSFER_IN = "transfer_in"
    TRANSFER_OUT = "transfer_out"
    TRADE = "trade"
    FEE = "fee"
    INTEREST = "interest"
    ADJUSTMENT = "adjustment"


class TransferState(str, Enum):
    CREATED = "created"
    APPROVED = "approved"
    RESERVED = "reserved"
    SUBMITTED = "submitted"
    SETTLED = "settled"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RETURNED = "returned"


@dataclass(frozen=True)
class CustodyAccount:
    account_id: str
    client_id: str
    currency: str
    name: str
    status: AccountStatus = AccountStatus.PENDING
    allow_overdraft: bool = False
    overdraft_limit: Decimal = Decimal("0")
    opened_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "account_id", text(self.account_id, "account id"))
        object.__setattr__(self, "client_id", text(self.client_id, "client id"))
        object.__setattr__(self, "currency", currency(self.currency))
        object.__setattr__(self, "name", text(self.name, "account name"))
        object.__setattr__(self, "status", AccountStatus(self.status))
        limit = money(self.overdraft_limit, "overdraft limit", non_negative=True)
        if not self.allow_overdraft and limit:
            raise ValidationError("overdraft limit requires overdraft permission")
        object.__setattr__(self, "overdraft_limit", limit)
        if self.opened_at is not None:
            object.__setattr__(self, "opened_at", utc(self.opened_at, "opened at"))
        if self.closed_at is not None:
            object.__setattr__(self, "closed_at", utc(self.closed_at, "closed at"))


@dataclass(frozen=True)
class CashMovement:
    movement_id: str
    account_id: str
    movement_type: MovementType
    amount: Decimal
    booked_at: datetime
    value_date: date
    source: str
    source_reference: str
    description: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "movement_id", text(self.movement_id, "movement id"))
        object.__setattr__(self, "account_id", text(self.account_id, "account id"))
        object.__setattr__(self, "movement_type", MovementType(self.movement_type))
        amount = money(self.amount, "movement amount")
        if amount == 0:
            raise ValidationError("movement amount cannot be zero")
        object.__setattr__(self, "amount", amount)
        object.__setattr__(self, "booked_at", utc(self.booked_at, "booked at"))
        object.__setattr__(self, "source", text(self.source, "movement source"))
        object.__setattr__(self, "source_reference", text(self.source_reference, "source reference"))


@dataclass(frozen=True)
class CashReservation:
    reservation_id: str
    account_id: str
    amount: Decimal
    reason: str
    reference: str
    created_at: datetime
    expires_at: datetime
    released_at: Optional[datetime] = None
    consumed_amount: Decimal = Decimal("0")
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "reservation_id", text(self.reservation_id, "reservation id"))
        object.__setattr__(self, "account_id", text(self.account_id, "account id"))
        amount = money(self.amount, "reservation amount", positive=True)
        consumed = money(self.consumed_amount, "consumed amount", non_negative=True)
        if consumed > amount:
            raise ValidationError("consumed amount exceeds reservation")
        object.__setattr__(self, "amount", amount)
        object.__setattr__(self, "consumed_amount", consumed)
        object.__setattr__(self, "reason", text(self.reason, "reservation reason"))
        object.__setattr__(self, "reference", text(self.reference, "reservation reference"))
        created = utc(self.created_at, "created at")
        expires = utc(self.expires_at, "expires at")
        if expires <= created:
            raise ValidationError("reservation expiry must follow creation")
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "expires_at", expires)
        if self.released_at is not None:
            object.__setattr__(self, "released_at", utc(self.released_at, "released at"))

    @property
    def remaining(self) -> Decimal:
        return self.amount - self.consumed_amount

    def active_at(self, at: datetime) -> bool:
        return self.released_at is None and at < self.expires_at and self.remaining > 0


@dataclass(frozen=True)
class CashTransfer:
    transfer_id: str
    source_account_id: str
    destination_account_id: str
    amount: Decimal
    currency: str
    requested_at: datetime
    requested_by: str
    state: TransferState = TransferState.CREATED
    reservation_id: Optional[str] = None
    external_reference: Optional[str] = None
    failure_reason: Optional[str] = None
    completed_at: Optional[datetime] = None
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "transfer_id", text(self.transfer_id, "transfer id"))
        object.__setattr__(self, "source_account_id", text(self.source_account_id, "source account id"))
        object.__setattr__(self, "destination_account_id", text(self.destination_account_id, "destination account id"))
        if self.source_account_id == self.destination_account_id:
            raise ValidationError("transfer accounts must differ")
        object.__setattr__(self, "amount", money(self.amount, "transfer amount", positive=True))
        object.__setattr__(self, "currency", currency(self.currency))
        object.__setattr__(self, "requested_at", utc(self.requested_at, "requested at"))
        object.__setattr__(self, "requested_by", text(self.requested_by, "requested by"))
        object.__setattr__(self, "state", TransferState(self.state))


@dataclass(frozen=True)
class AccountBalance:
    account_id: str
    booked: Decimal
    valued: Decimal
    reserved: Decimal
    available: Decimal
    as_of: datetime
