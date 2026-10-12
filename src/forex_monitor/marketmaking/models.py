"""Validated values for electronic FX market making."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Mapping, Optional, Tuple

from forex_monitor.errors import ValidationError


def text(value: object, name: str) -> str:
    result = str(value).strip() if value is not None else ""
    if not result:
        raise ValidationError("%s is required" % name)
    return result


def number(
    value: object,
    name: str,
    *,
    positive: bool = False,
    non_negative: bool = False,
) -> Decimal:
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


def timestamp(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError("%s must be timezone-aware" % name)
    return value.astimezone(timezone.utc)


def pair(value: str) -> str:
    result = text(value, "symbol").upper().replace("/", "")
    if len(result) != 6 or not result.isalpha():
        raise ValidationError("symbol must be a six-letter currency pair")
    return result


class BookSide(str, Enum):
    BID = "bid"
    ASK = "ask"


class QuoteState(str, Enum):
    LIVE = "live"
    FILLED = "filled"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class HedgeState(str, Enum):
    PLANNED = "planned"
    ROUTED = "routed"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True)
class BookLevel:
    venue: str
    symbol: str
    side: BookSide
    price: Decimal
    quantity: Decimal
    observed_at: datetime
    source_sequence: int
    order_count: int = 1
    firm: bool = True
    minimum_quantity: Decimal = Decimal("0")
    metadata: Tuple[Tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "venue", text(self.venue, "venue").upper())
        object.__setattr__(self, "symbol", pair(self.symbol))
        object.__setattr__(self, "side", BookSide(self.side))
        object.__setattr__(self, "price", number(self.price, "price", positive=True))
        # Incremental venue books use a zero quantity to remove one price level.
        object.__setattr__(self, "quantity", number(self.quantity, "quantity", non_negative=True))
        object.__setattr__(self, "observed_at", timestamp(self.observed_at, "observed at"))
        if self.source_sequence < 0:
            raise ValidationError("source sequence cannot be negative")
        if self.order_count < 1:
            raise ValidationError("order count must be positive")
        minimum = number(self.minimum_quantity, "minimum quantity", non_negative=True)
        if minimum > self.quantity:
            raise ValidationError("minimum quantity cannot exceed level quantity")
        object.__setattr__(self, "minimum_quantity", minimum)
        object.__setattr__(self, "metadata", tuple(sorted(self.metadata)))


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    sequence: int
    bids: Tuple[BookLevel, ...]
    asks: Tuple[BookLevel, ...]
    observed_at: datetime
    stale_venues: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        symbol = pair(self.symbol)
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "observed_at", timestamp(self.observed_at, "observed at"))
        if self.sequence < 1:
            raise ValidationError("snapshot sequence must be positive")
        if any(level.symbol != symbol for level in self.bids + self.asks):
            raise ValidationError("snapshot levels must use one symbol")
        if any(level.side != BookSide.BID for level in self.bids):
            raise ValidationError("snapshot bid collection contains another side")
        if any(level.side != BookSide.ASK for level in self.asks):
            raise ValidationError("snapshot ask collection contains another side")
        if tuple(sorted(self.bids, key=lambda level: level.price, reverse=True)) != self.bids:
            raise ValidationError("snapshot bids must be price sorted")
        if tuple(sorted(self.asks, key=lambda level: level.price)) != self.asks:
            raise ValidationError("snapshot asks must be price sorted")
        object.__setattr__(self, "stale_venues", tuple(sorted(set(self.stale_venues))))

    @property
    def best_bid(self) -> Optional[Decimal]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[Decimal]:
        return self.asks[0].price if self.asks else None

    @property
    def midpoint(self) -> Optional[Decimal]:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / Decimal("2")

    @property
    def spread(self) -> Optional[Decimal]:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid


@dataclass(frozen=True)
class DealerQuote:
    quote_id: str
    request_id: str
    client_id: str
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    created_at: datetime
    expires_at: datetime
    state: QuoteState = QuoteState.LIVE
    filled_quantity: Decimal = Decimal("0")
    skew_pips: Decimal = Decimal("0")
    spread_pips: Decimal = Decimal("0")
    reference_price: Optional[Decimal] = None
    strategy: str = "default"
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "quote_id", text(self.quote_id, "quote id"))
        object.__setattr__(self, "request_id", text(self.request_id, "request id"))
        object.__setattr__(self, "client_id", text(self.client_id, "client id"))
        object.__setattr__(self, "symbol", pair(self.symbol))
        side = text(self.side, "side").lower()
        if side not in {"buy", "sell"}:
            raise ValidationError("quote side must be buy or sell")
        object.__setattr__(self, "side", side)
        quantity = number(self.quantity, "quantity", positive=True)
        filled = number(self.filled_quantity, "filled quantity", non_negative=True)
        if filled > quantity:
            raise ValidationError("filled quantity cannot exceed quote quantity")
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "filled_quantity", filled)
        object.__setattr__(self, "price", number(self.price, "price", positive=True))
        created = timestamp(self.created_at, "created at")
        expires = timestamp(self.expires_at, "expires at")
        if expires <= created:
            raise ValidationError("quote expiry must follow creation")
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "expires_at", expires)
        object.__setattr__(self, "state", QuoteState(self.state))
        object.__setattr__(self, "skew_pips", number(self.skew_pips, "skew pips"))
        object.__setattr__(self, "spread_pips", number(self.spread_pips, "spread pips", non_negative=True))
        if self.reference_price is not None:
            object.__setattr__(self, "reference_price", number(self.reference_price, "reference price", positive=True))

    @property
    def remaining_quantity(self) -> Decimal:
        return self.quantity - self.filled_quantity


@dataclass(frozen=True)
class InventoryPosition:
    currency: str
    quantity: Decimal
    average_cost: Decimal
    realized_pnl: Decimal = Decimal("0")
    funding_pnl: Decimal = Decimal("0")
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    version: int = 1

    def __post_init__(self) -> None:
        code = text(self.currency, "currency").upper()
        if len(code) != 3 or not code.isalpha():
            raise ValidationError("inventory currency is invalid")
        object.__setattr__(self, "currency", code)
        object.__setattr__(self, "quantity", number(self.quantity, "quantity"))
        object.__setattr__(self, "average_cost", number(self.average_cost, "average cost", non_negative=True))
        object.__setattr__(self, "realized_pnl", number(self.realized_pnl, "realized PnL"))
        object.__setattr__(self, "funding_pnl", number(self.funding_pnl, "funding PnL"))
        object.__setattr__(self, "updated_at", timestamp(self.updated_at, "updated at"))


@dataclass(frozen=True)
class HedgeOrder:
    hedge_id: str
    currency: str
    side: str
    quantity: Decimal
    target_currency: str
    symbol: str
    limit_price: Optional[Decimal]
    venue: Optional[str]
    reason: str
    created_at: datetime
    state: HedgeState = HedgeState.PLANNED
    filled_quantity: Decimal = Decimal("0")
    average_fill_price: Optional[Decimal] = None
    version: int = 1

    def __post_init__(self) -> None:
        source = text(self.currency, "currency").upper()
        target = text(self.target_currency, "target currency").upper()
        if len(source) != 3 or len(target) != 3 or source == target:
            raise ValidationError("hedge currencies are invalid")
        object.__setattr__(self, "currency", source)
        object.__setattr__(self, "target_currency", target)
        object.__setattr__(self, "symbol", pair(self.symbol))
        side = text(self.side, "side").lower()
        if side not in {"buy", "sell"}:
            raise ValidationError("hedge side must be buy or sell")
        object.__setattr__(self, "side", side)
        quantity = number(self.quantity, "hedge quantity", positive=True)
        filled = number(self.filled_quantity, "filled quantity", non_negative=True)
        if filled > quantity:
            raise ValidationError("hedge fill exceeds quantity")
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "filled_quantity", filled)
        if self.limit_price is not None:
            object.__setattr__(self, "limit_price", number(self.limit_price, "limit price", positive=True))
        if self.average_fill_price is not None:
            object.__setattr__(self, "average_fill_price", number(self.average_fill_price, "average fill price", positive=True))
        object.__setattr__(self, "reason", text(self.reason, "hedge reason"))
        object.__setattr__(self, "created_at", timestamp(self.created_at, "created at"))
        object.__setattr__(self, "state", HedgeState(self.state))


@dataclass(frozen=True)
class DealerFill:
    fill_id: str
    quote_id: str
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    filled_at: datetime
    client_id: str
    venue: str = "INTERNAL"
    metadata: Tuple[Tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "fill_id", text(self.fill_id, "fill id"))
        object.__setattr__(self, "quote_id", text(self.quote_id, "quote id"))
        object.__setattr__(self, "symbol", pair(self.symbol))
        side = text(self.side, "side").lower()
        if side not in {"buy", "sell"}:
            raise ValidationError("fill side must be buy or sell")
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "quantity", number(self.quantity, "quantity", positive=True))
        object.__setattr__(self, "price", number(self.price, "price", positive=True))
        object.__setattr__(self, "filled_at", timestamp(self.filled_at, "filled at"))
        object.__setattr__(self, "client_id", text(self.client_id, "client id"))
        object.__setattr__(self, "venue", text(self.venue, "venue").upper())
        object.__setattr__(self, "metadata", tuple(sorted(self.metadata)))
