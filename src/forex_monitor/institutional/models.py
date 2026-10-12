"""Immutable values shared by institutional dealing workflows."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Mapping, Optional, Tuple

from forex_monitor.errors import ValidationError


def required(value: object, field_name: str) -> str:
    text = str(value).strip() if value is not None else ""
    if not text:
        raise ValidationError("%s is required" % field_name)
    return text


def currency(value: object, field_name: str = "currency") -> str:
    text = required(value, field_name).upper()
    if len(text) != 3 or not text.isalpha():
        raise ValidationError("%s must be a three-letter code" % field_name)
    return text


def amount(value: object, field_name: str, *, allow_zero: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError("%s must be numeric" % field_name) from exc
    if not result.is_finite() or result < 0 or (not allow_zero and result == 0):
        raise ValidationError("%s must be %s" % (field_name, "non-negative" if allow_zero else "positive"))
    return result


def aware(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError("%s must be timezone-aware" % field_name)
    return value.astimezone(timezone.utc)


def metadata(value: Mapping[str, object]) -> Tuple[Tuple[str, str], ...]:
    return tuple(sorted((required(key, "metadata key"), str(item)) for key, item in value.items()))


class ClientStatus(str, Enum):
    PROSPECT = "prospect"
    ONBOARDING = "onboarding"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    CLOSED = "closed"


class AgreementStatus(str, Enum):
    DRAFT = "draft"
    REVIEW = "review"
    EXECUTED = "executed"
    TERMINATED = "terminated"
    EXPIRED = "expired"


class RfqStatus(str, Enum):
    OPEN = "open"
    QUOTED = "quoted"
    ACCEPTED = "accepted"
    PASSED = "passed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class TradeState(str, Enum):
    CAPTURED = "captured"
    VALIDATED = "validated"
    CONFIRMED = "confirmed"
    MATCHED = "matched"
    SETTLEMENT_PENDING = "settlement_pending"
    SETTLED = "settled"
    CANCELLED = "cancelled"
    FAILED = "failed"


class ConfirmationStatus(str, Enum):
    DRAFT = "draft"
    SENT = "sent"
    AFFIRMED = "affirmed"
    DISPUTED = "disputed"
    SUPERSEDED = "superseded"


class SettlementStatus(str, Enum):
    DRAFT = "draft"
    RELEASED = "released"
    ACKNOWLEDGED = "acknowledged"
    SETTLED = "settled"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class ClientAccount:
    client_id: str
    legal_name: str
    lei: str
    country: str
    base_currency: str
    classification: str
    status: ClientStatus = ClientStatus.PROSPECT
    trading_permissions: Tuple[str, ...] = ()
    settlement_accounts: Tuple[str, ...] = ()
    parent_id: Optional[str] = None
    risk_rating: str = "medium"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "client_id", required(self.client_id, "client id"))
        object.__setattr__(self, "legal_name", required(self.legal_name, "legal name"))
        lei = required(self.lei, "LEI").upper()
        if len(lei) != 20 or not lei.isalnum():
            raise ValidationError("LEI must contain twenty alphanumeric characters")
        object.__setattr__(self, "lei", lei)
        object.__setattr__(self, "country", required(self.country, "country").upper())
        object.__setattr__(self, "base_currency", currency(self.base_currency, "base currency"))
        object.__setattr__(self, "classification", required(self.classification, "classification").lower())
        object.__setattr__(self, "status", ClientStatus(self.status))
        object.__setattr__(self, "trading_permissions", tuple(sorted(set(self.trading_permissions))))
        object.__setattr__(self, "settlement_accounts", tuple(sorted(set(self.settlement_accounts))))
        object.__setattr__(self, "parent_id", self.parent_id.strip() if self.parent_id else None)
        risk = required(self.risk_rating, "risk rating").lower()
        if risk not in {"low", "medium", "high", "prohibited"}:
            raise ValidationError("risk rating is not supported")
        object.__setattr__(self, "risk_rating", risk)
        object.__setattr__(self, "created_at", aware(self.created_at, "created at"))
        if self.version < 1:
            raise ValidationError("client version must be positive")


@dataclass(frozen=True)
class LegalAgreement:
    agreement_id: str
    client_id: str
    agreement_type: str
    status: AgreementStatus
    effective_from: date
    effective_to: Optional[date] = None
    products: Tuple[str, ...] = ()
    currencies: Tuple[str, ...] = ()
    governing_law: str = ""
    netting_set_id: Optional[str] = None
    collateral_terms_id: Optional[str] = None
    signed_by: Tuple[str, ...] = ()
    document_hash: Optional[str] = None
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "agreement_id", required(self.agreement_id, "agreement id"))
        object.__setattr__(self, "client_id", required(self.client_id, "client id"))
        object.__setattr__(self, "agreement_type", required(self.agreement_type, "agreement type").lower())
        object.__setattr__(self, "status", AgreementStatus(self.status))
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValidationError("agreement end cannot precede its start")
        object.__setattr__(self, "products", tuple(sorted(set(item.lower() for item in self.products))))
        object.__setattr__(self, "currencies", tuple(sorted(set(currency(item) for item in self.currencies))))
        object.__setattr__(self, "governing_law", required(self.governing_law, "governing law"))
        document_hash = self.document_hash.lower() if self.document_hash else None
        if document_hash is not None and (len(document_hash) != 64 or any(char not in "0123456789abcdef" for char in document_hash)):
            raise ValidationError("agreement document hash must be SHA-256")
        object.__setattr__(self, "document_hash", document_hash)
        object.__setattr__(self, "signed_by", tuple(sorted(set(self.signed_by))))

    def is_effective(self, on: date) -> bool:
        return self.status == AgreementStatus.EXECUTED and self.effective_from <= on and (self.effective_to is None or on <= self.effective_to)


@dataclass(frozen=True)
class QuoteRequest:
    rfq_id: str
    client_id: str
    symbol: str
    side: str
    notional: Decimal
    notional_currency: str
    settlement_date: date
    requested_at: datetime
    expires_at: datetime
    status: RfqStatus = RfqStatus.OPEN
    quoted_price: Optional[Decimal] = None
    quoted_at: Optional[datetime] = None
    accepted_at: Optional[datetime] = None
    dealer_id: Optional[str] = None
    request_key: Optional[str] = None
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "rfq_id", required(self.rfq_id, "RFQ id"))
        object.__setattr__(self, "client_id", required(self.client_id, "client id"))
        symbol = required(self.symbol, "symbol").upper().replace("/", "")
        if len(symbol) != 6 or not symbol.isalpha():
            raise ValidationError("RFQ symbol must be a currency pair")
        object.__setattr__(self, "symbol", symbol)
        side = required(self.side, "side").lower()
        if side not in {"buy", "sell"}:
            raise ValidationError("RFQ side must be buy or sell")
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "notional", amount(self.notional, "notional"))
        object.__setattr__(self, "notional_currency", currency(self.notional_currency, "notional currency"))
        requested = aware(self.requested_at, "requested at")
        expires = aware(self.expires_at, "expires at")
        if expires <= requested:
            raise ValidationError("RFQ expiry must follow request time")
        object.__setattr__(self, "requested_at", requested)
        object.__setattr__(self, "expires_at", expires)
        object.__setattr__(self, "status", RfqStatus(self.status))
        if self.quoted_price is not None:
            object.__setattr__(self, "quoted_price", amount(self.quoted_price, "quoted price"))
        if self.quoted_at is not None:
            object.__setattr__(self, "quoted_at", aware(self.quoted_at, "quoted at"))
        if self.accepted_at is not None:
            object.__setattr__(self, "accepted_at", aware(self.accepted_at, "accepted at"))


@dataclass(frozen=True)
class InstitutionalTrade:
    trade_id: str
    client_id: str
    rfq_id: Optional[str]
    symbol: str
    side: str
    base_amount: Decimal
    quote_amount: Decimal
    price: Decimal
    trade_date: date
    settlement_date: date
    booked_at: datetime
    state: TradeState = TradeState.CAPTURED
    dealer_id: Optional[str] = None
    venue: str = "OTC"
    agreement_id: Optional[str] = None
    allocation_account: Optional[str] = None
    external_reference: Optional[str] = None
    metadata: Tuple[Tuple[str, str], ...] = ()
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "trade_id", required(self.trade_id, "trade id"))
        object.__setattr__(self, "client_id", required(self.client_id, "client id"))
        symbol = required(self.symbol, "symbol").upper().replace("/", "")
        if len(symbol) != 6 or not symbol.isalpha():
            raise ValidationError("trade symbol must be a currency pair")
        object.__setattr__(self, "symbol", symbol)
        side = required(self.side, "side").lower()
        if side not in {"buy", "sell"}:
            raise ValidationError("trade side must be buy or sell")
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "base_amount", amount(self.base_amount, "base amount"))
        object.__setattr__(self, "quote_amount", amount(self.quote_amount, "quote amount"))
        object.__setattr__(self, "price", amount(self.price, "price"))
        if self.settlement_date < self.trade_date:
            raise ValidationError("settlement date cannot precede trade date")
        object.__setattr__(self, "booked_at", aware(self.booked_at, "booked at"))
        object.__setattr__(self, "state", TradeState(self.state))
        object.__setattr__(self, "venue", required(self.venue, "venue").upper())
        object.__setattr__(self, "metadata", tuple(sorted(self.metadata)))

    @property
    def base_currency(self) -> str:
        return self.symbol[:3]

    @property
    def quote_currency(self) -> str:
        return self.symbol[3:]


@dataclass(frozen=True)
class Confirmation:
    confirmation_id: str
    trade_id: str
    revision: int
    status: ConfirmationStatus
    generated_at: datetime
    terms_hash: str
    sent_at: Optional[datetime] = None
    affirmed_at: Optional[datetime] = None
    disputed_fields: Tuple[str, ...] = ()
    counterparty_reference: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "confirmation_id", required(self.confirmation_id, "confirmation id"))
        object.__setattr__(self, "trade_id", required(self.trade_id, "trade id"))
        if self.revision < 1:
            raise ValidationError("confirmation revision must be positive")
        object.__setattr__(self, "status", ConfirmationStatus(self.status))
        object.__setattr__(self, "generated_at", aware(self.generated_at, "generated at"))
        digest = required(self.terms_hash, "terms hash").lower()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValidationError("confirmation terms hash must be SHA-256")
        object.__setattr__(self, "terms_hash", digest)
        object.__setattr__(self, "disputed_fields", tuple(sorted(set(self.disputed_fields))))


@dataclass(frozen=True)
class SettlementInstruction:
    instruction_id: str
    trade_id: str
    currency: str
    amount: Decimal
    direction: str
    value_date: date
    account_id: str
    correspondent: Optional[str] = None
    status: SettlementStatus = SettlementStatus.DRAFT
    released_at: Optional[datetime] = None
    settled_at: Optional[datetime] = None
    failure_reason: Optional[str] = None
    external_reference: Optional[str] = None
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "instruction_id", required(self.instruction_id, "instruction id"))
        object.__setattr__(self, "trade_id", required(self.trade_id, "trade id"))
        object.__setattr__(self, "currency", currency(self.currency))
        object.__setattr__(self, "amount", amount(self.amount, "settlement amount"))
        direction = required(self.direction, "direction").lower()
        if direction not in {"pay", "receive"}:
            raise ValidationError("settlement direction must be pay or receive")
        object.__setattr__(self, "direction", direction)
        object.__setattr__(self, "account_id", required(self.account_id, "settlement account"))
        object.__setattr__(self, "status", SettlementStatus(self.status))


@dataclass(frozen=True)
class LifecycleEvent:
    event_id: str
    trade_id: str
    from_state: TradeState
    to_state: TradeState
    actor_id: str
    reason: str
    occurred_at: datetime
    details: Tuple[Tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", required(self.event_id, "event id"))
        object.__setattr__(self, "trade_id", required(self.trade_id, "trade id"))
        object.__setattr__(self, "from_state", TradeState(self.from_state))
        object.__setattr__(self, "to_state", TradeState(self.to_state))
        object.__setattr__(self, "actor_id", required(self.actor_id, "actor id"))
        object.__setattr__(self, "reason", required(self.reason, "event reason"))
        object.__setattr__(self, "occurred_at", aware(self.occurred_at, "event time"))
        object.__setattr__(self, "details", tuple(sorted(self.details)))


def evolve(value: object, **changes: object) -> object:
    """Typed wrapper around dataclasses.replace used by services."""

    return replace(value, **changes)
