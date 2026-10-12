"""Trading venues, executable quotes, and availability controls."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Dict, Iterable, Mapping, Optional, Tuple

from forex_monitor.errors import NotFoundError, ValidationError
from forex_monitor.models import as_decimal, ensure_utc, normalize_symbol


class VenueState(str, Enum):
    ACTIVE = "active"
    DEGRADED = "degraded"
    HALTED = "halted"
    DISCONNECTED = "disconnected"


@dataclass(frozen=True)
class Venue:
    venue_id: str
    name: str
    priority: int = 100
    maker_fee_bps: Decimal = Decimal("0")
    taker_fee_bps: Decimal = Decimal("0")
    minimum_quantity: Decimal = Decimal("1")
    maximum_quantity: Optional[Decimal] = None
    supported_symbols: Tuple[str, ...] = ()
    state: VenueState = VenueState.ACTIVE
    last_heartbeat: Optional[datetime] = None

    def __post_init__(self) -> None:
        venue_id = self.venue_id.strip()
        name = self.name.strip()
        if not venue_id or not name:
            raise ValidationError("venue id and name cannot be blank")
        if self.priority < 0:
            raise ValidationError("venue priority cannot be negative")
        minimum = as_decimal(self.minimum_quantity, "minimum quantity", positive=True)
        maximum = as_decimal(self.maximum_quantity, "maximum quantity", positive=True) if self.maximum_quantity is not None else None
        if maximum is not None and maximum < minimum:
            raise ValidationError("maximum venue quantity cannot be below minimum")
        symbols = tuple(sorted({normalize_symbol(symbol) for symbol in self.supported_symbols}))
        state = self.state if isinstance(self.state, VenueState) else VenueState(self.state)
        heartbeat = ensure_utc(self.last_heartbeat, "venue heartbeat") if self.last_heartbeat else None
        object.__setattr__(self, "venue_id", venue_id)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "minimum_quantity", minimum)
        object.__setattr__(self, "maximum_quantity", maximum)
        object.__setattr__(self, "supported_symbols", symbols)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "last_heartbeat", heartbeat)
        object.__setattr__(self, "maker_fee_bps", as_decimal(self.maker_fee_bps, "maker fee"))
        object.__setattr__(self, "taker_fee_bps", as_decimal(self.taker_fee_bps, "taker fee"))

    def supports(self, symbol: str, quantity: object) -> bool:
        normalized = normalize_symbol(symbol)
        parsed = as_decimal(quantity, "quantity", positive=True)
        return (
            self.state in {VenueState.ACTIVE, VenueState.DEGRADED}
            and (not self.supported_symbols or normalized in self.supported_symbols)
            and parsed >= self.minimum_quantity
            and (self.maximum_quantity is None or parsed <= self.maximum_quantity)
        )

    def effective_price(self, price: Decimal, *, taker: bool, buy: bool) -> Decimal:
        fee = self.taker_fee_bps if taker else self.maker_fee_bps
        multiplier = Decimal("1") + (fee / Decimal("10000") if buy else -fee / Decimal("10000"))
        return price * multiplier


@dataclass(frozen=True)
class VenueQuote:
    venue_id: str
    symbol: str
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal
    observed_at: datetime
    quote_id: Optional[str] = None

    def __post_init__(self) -> None:
        venue = self.venue_id.strip()
        if not venue:
            raise ValidationError("quote venue id cannot be blank")
        bid = as_decimal(self.bid, "bid", positive=True)
        ask = as_decimal(self.ask, "ask", positive=True)
        if ask < bid:
            raise ValidationError("quote ask cannot be below bid")
        object.__setattr__(self, "venue_id", venue)
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(self, "bid", bid)
        object.__setattr__(self, "ask", ask)
        object.__setattr__(self, "bid_size", as_decimal(self.bid_size, "bid size", positive=True))
        object.__setattr__(self, "ask_size", as_decimal(self.ask_size, "ask size", positive=True))
        object.__setattr__(self, "observed_at", ensure_utc(self.observed_at, "quote time"))

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / Decimal("2")

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid

    def stale(self, now: datetime, maximum_age: timedelta) -> bool:
        timestamp = ensure_utc(now, "quote comparison time")
        if maximum_age.total_seconds() < 0:
            raise ValidationError("maximum quote age cannot be negative")
        return timestamp - self.observed_at > maximum_age


class VenueBook:
    def __init__(self) -> None:
        self._venues: Dict[str, Venue] = {}
        self._quotes: Dict[Tuple[str, str], VenueQuote] = {}

    def register(self, venue: Venue) -> None:
        if venue.venue_id in self._venues:
            raise ValidationError("venue is already registered")
        self._venues[venue.venue_id] = venue

    def venue(self, venue_id: str) -> Venue:
        try:
            return self._venues[venue_id]
        except KeyError as exc:
            raise NotFoundError("venue was not found") from exc

    def update_state(self, venue_id: str, state: VenueState, at: datetime) -> Venue:
        venue = self.venue(venue_id)
        timestamp = ensure_utc(at, "venue state time")
        if venue.last_heartbeat and timestamp < venue.last_heartbeat:
            raise ValidationError("venue state time cannot move backward")
        changed = replace(venue, state=state, last_heartbeat=timestamp)
        self._venues[venue_id] = changed
        return changed

    def quote(self, quote: VenueQuote) -> None:
        venue = self.venue(quote.venue_id)
        if venue.supported_symbols and quote.symbol not in venue.supported_symbols:
            raise ValidationError("venue quote uses an unsupported symbol")
        key = (quote.venue_id, quote.symbol)
        existing = self._quotes.get(key)
        if existing is not None and quote.observed_at < existing.observed_at:
            raise ValidationError("venue quote time cannot move backward")
        self._quotes[key] = quote

    def executable(
        self,
        symbol: str,
        quantity: object,
        now: datetime,
        maximum_age: timedelta,
    ) -> Tuple[Tuple[Venue, VenueQuote], ...]:
        normalized = normalize_symbol(symbol)
        result = []
        for venue in self._venues.values():
            quote = self._quotes.get((venue.venue_id, normalized))
            if quote is None or quote.stale(now, maximum_age) or not venue.supports(normalized, quantity):
                continue
            result.append((venue, quote))
        return tuple(sorted(result, key=lambda item: (item[0].priority, item[0].venue_id)))

    def snapshot(self) -> Mapping[str, Mapping[str, object]]:
        return {
            venue_id: {
                "name": venue.name,
                "state": venue.state.value,
                "priority": venue.priority,
                "symbols": venue.supported_symbols,
            }
            for venue_id, venue in sorted(self._venues.items())
        }
