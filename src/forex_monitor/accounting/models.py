"""Accounting value objects with strict monetary validation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Mapping, Optional, Tuple

from forex_monitor.errors import ValidationError


def text(value: object, name: str, maximum: Optional[int] = None) -> str:
    result = str(value).strip() if value is not None else ""
    if not result:
        raise ValidationError("%s is required" % name)
    if maximum is not None and len(result) > maximum:
        raise ValidationError("%s exceeds maximum length" % name)
    return result


def money(value: object, name: str, *, non_negative: bool = False, positive: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError("%s must be numeric" % name) from exc
    if not result.is_finite():
        raise ValidationError("%s must be finite" % name)
    if non_negative and result < 0:
        raise ValidationError("%s cannot be negative" % name)
    if positive and result <= 0:
        raise ValidationError("%s must be positive" % name)
    return result


def currency(value: object) -> str:
    result = text(value, "currency").upper()
    if len(result) != 3 or not result.isalpha():
        raise ValidationError("currency must be a three-letter code")
    return result


def utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError("%s must be timezone-aware" % name)
    return value.astimezone(timezone.utc)


class AccountType(str, Enum):
    ASSET = "asset"
    LIABILITY = "liability"
    EQUITY = "equity"
    REVENUE = "revenue"
    EXPENSE = "expense"


class EntryStatus(str, Enum):
    DRAFT = "draft"
    POSTED = "posted"
    REVERSED = "reversed"


class InvoiceStatus(str, Enum):
    DRAFT = "draft"
    ISSUED = "issued"
    PARTIALLY_PAID = "partially_paid"
    PAID = "paid"
    VOID = "void"
    OVERDUE = "overdue"


@dataclass(frozen=True)
class Account:
    account_id: str
    code: str
    name: str
    account_type: AccountType
    currency: str
    parent_id: Optional[str] = None
    allow_manual_posting: bool = True
    active: bool = True
    tags: Tuple[str, ...] = ()
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "account_id", text(self.account_id, "account id", 80))
        object.__setattr__(self, "code", text(self.code, "account code", 32).upper())
        object.__setattr__(self, "name", text(self.name, "account name", 160))
        object.__setattr__(self, "account_type", AccountType(self.account_type))
        object.__setattr__(self, "currency", currency(self.currency))
        object.__setattr__(self, "parent_id", self.parent_id.strip() if self.parent_id else None)
        object.__setattr__(self, "tags", tuple(sorted(set(item.strip().lower() for item in self.tags if item.strip()))))
        if self.version < 1:
            raise ValidationError("account version must be positive")

    @property
    def normal_side(self) -> str:
        return "debit" if self.account_type in {AccountType.ASSET, AccountType.EXPENSE} else "credit"


@dataclass(frozen=True)
class JournalLine:
    line_id: str
    account_id: str
    debit: Decimal = Decimal("0")
    credit: Decimal = Decimal("0")
    description: str = ""
    dimensions: Tuple[Tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "line_id", text(self.line_id, "line id"))
        object.__setattr__(self, "account_id", text(self.account_id, "account id"))
        debit = money(self.debit, "debit", non_negative=True)
        credit = money(self.credit, "credit", non_negative=True)
        if (debit == 0) == (credit == 0):
            raise ValidationError("journal line must contain exactly one debit or credit")
        object.__setattr__(self, "debit", debit)
        object.__setattr__(self, "credit", credit)
        object.__setattr__(self, "description", self.description.strip())
        keys = [key for key, _ in self.dimensions]
        if len(keys) != len(set(keys)):
            raise ValidationError("journal dimensions contain duplicate keys")
        object.__setattr__(self, "dimensions", tuple(sorted((text(k, "dimension"), str(v)) for k, v in self.dimensions)))


@dataclass(frozen=True)
class JournalEntry:
    entry_id: str
    journal: str
    accounting_date: date
    currency: str
    description: str
    lines: Tuple[JournalLine, ...]
    created_at: datetime
    status: EntryStatus = EntryStatus.DRAFT
    source: str = "manual"
    source_reference: Optional[str] = None
    posted_at: Optional[datetime] = None
    reversal_of: Optional[str] = None
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "entry_id", text(self.entry_id, "entry id"))
        object.__setattr__(self, "journal", text(self.journal, "journal").upper())
        object.__setattr__(self, "currency", currency(self.currency))
        object.__setattr__(self, "description", text(self.description, "entry description", 500))
        if len(self.lines) < 2:
            raise ValidationError("journal entry requires at least two lines")
        line_ids = [line.line_id for line in self.lines]
        if len(line_ids) != len(set(line_ids)):
            raise ValidationError("journal entry contains duplicate line ids")
        debit = sum((line.debit for line in self.lines), Decimal("0"))
        credit = sum((line.credit for line in self.lines), Decimal("0"))
        if debit != credit:
            raise ValidationError("journal entry is not balanced")
        object.__setattr__(self, "created_at", utc(self.created_at, "created at"))
        object.__setattr__(self, "status", EntryStatus(self.status))
        object.__setattr__(self, "source", text(self.source, "source").lower())
        if self.posted_at is not None:
            posted = utc(self.posted_at, "posted at")
            if posted < self.created_at:
                raise ValidationError("posting time cannot precede creation")
            object.__setattr__(self, "posted_at", posted)
        if self.status == EntryStatus.POSTED and self.posted_at is None:
            raise ValidationError("posted entry requires a posting time")
        if self.version < 1:
            raise ValidationError("entry version must be positive")

    @property
    def amount(self) -> Decimal:
        return sum((line.debit for line in self.lines), Decimal("0"))


@dataclass(frozen=True)
class FeeCharge:
    charge_id: str
    schedule_id: str
    client_id: str
    charge_type: str
    currency: str
    quantity: Decimal
    rate: Decimal
    amount: Decimal
    occurred_at: datetime
    source_reference: str
    description: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "charge_id", text(self.charge_id, "charge id"))
        object.__setattr__(self, "schedule_id", text(self.schedule_id, "schedule id"))
        object.__setattr__(self, "client_id", text(self.client_id, "client id"))
        object.__setattr__(self, "charge_type", text(self.charge_type, "charge type").lower())
        object.__setattr__(self, "currency", currency(self.currency))
        object.__setattr__(self, "quantity", money(self.quantity, "quantity", non_negative=True))
        object.__setattr__(self, "rate", money(self.rate, "rate", non_negative=True))
        object.__setattr__(self, "amount", money(self.amount, "amount", non_negative=True))
        object.__setattr__(self, "occurred_at", utc(self.occurred_at, "occurred at"))
        object.__setattr__(self, "source_reference", text(self.source_reference, "source reference"))
        object.__setattr__(self, "description", self.description.strip())


@dataclass(frozen=True)
class InvoiceLine:
    line_id: str
    description: str
    quantity: Decimal
    unit_price: Decimal
    tax_rate: Decimal = Decimal("0")
    charge_ids: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "line_id", text(self.line_id, "invoice line id"))
        object.__setattr__(self, "description", text(self.description, "invoice line description"))
        object.__setattr__(self, "quantity", money(self.quantity, "invoice quantity", positive=True))
        object.__setattr__(self, "unit_price", money(self.unit_price, "unit price", non_negative=True))
        tax = money(self.tax_rate, "tax rate", non_negative=True)
        if tax > 1:
            raise ValidationError("tax rate cannot exceed one")
        object.__setattr__(self, "tax_rate", tax)
        object.__setattr__(self, "charge_ids", tuple(sorted(set(self.charge_ids))))

    @property
    def subtotal(self) -> Decimal:
        return self.quantity * self.unit_price

    @property
    def tax(self) -> Decimal:
        return self.subtotal * self.tax_rate

    @property
    def total(self) -> Decimal:
        return self.subtotal + self.tax


@dataclass(frozen=True)
class Invoice:
    invoice_id: str
    client_id: str
    currency: str
    issue_date: date
    due_date: date
    lines: Tuple[InvoiceLine, ...]
    status: InvoiceStatus = InvoiceStatus.DRAFT
    paid_amount: Decimal = Decimal("0")
    issued_at: Optional[datetime] = None
    void_reason: Optional[str] = None
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "invoice_id", text(self.invoice_id, "invoice id"))
        object.__setattr__(self, "client_id", text(self.client_id, "client id"))
        object.__setattr__(self, "currency", currency(self.currency))
        if self.due_date < self.issue_date:
            raise ValidationError("invoice due date cannot precede issue date")
        if not self.lines:
            raise ValidationError("invoice requires at least one line")
        ids = [line.line_id for line in self.lines]
        if len(ids) != len(set(ids)):
            raise ValidationError("invoice contains duplicate line ids")
        object.__setattr__(self, "status", InvoiceStatus(self.status))
        paid = money(self.paid_amount, "paid amount", non_negative=True)
        if paid > self.total:
            raise ValidationError("invoice payment exceeds total")
        object.__setattr__(self, "paid_amount", paid)
        if self.issued_at is not None:
            object.__setattr__(self, "issued_at", utc(self.issued_at, "issued at"))
        if self.status != InvoiceStatus.DRAFT and self.status != InvoiceStatus.VOID and self.issued_at is None:
            raise ValidationError("issued invoice requires issue timestamp")
        if self.version < 1:
            raise ValidationError("invoice version must be positive")

    @property
    def subtotal(self) -> Decimal:
        return sum((line.subtotal for line in self.lines), Decimal("0"))

    @property
    def tax(self) -> Decimal:
        return sum((line.tax for line in self.lines), Decimal("0"))

    @property
    def total(self) -> Decimal:
        return self.subtotal + self.tax

    @property
    def outstanding(self) -> Decimal:
        return self.total - self.paid_amount


@dataclass(frozen=True)
class Payment:
    payment_id: str
    invoice_id: str
    amount: Decimal
    currency: str
    received_at: datetime
    reference: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "payment_id", text(self.payment_id, "payment id"))
        object.__setattr__(self, "invoice_id", text(self.invoice_id, "invoice id"))
        object.__setattr__(self, "amount", money(self.amount, "payment amount", positive=True))
        object.__setattr__(self, "currency", currency(self.currency))
        object.__setattr__(self, "received_at", utc(self.received_at, "received at"))
        object.__setattr__(self, "reference", text(self.reference, "payment reference"))
