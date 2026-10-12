"""Consolidated multi-venue order books with sequence recovery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from threading import RLock
from typing import Dict, Iterable, Mapping, Optional, Tuple

from forex_monitor.errors import StorageConflictError, ValidationError
from forex_monitor.marketmaking.models import (
    BookLevel,
    BookSide,
    MarketSnapshot,
    pair,
    text,
    timestamp,
)


@dataclass(frozen=True)
class VenueBookUpdate:
    update_id: str
    venue: str
    symbol: str
    sequence: int
    observed_at: datetime
    bids: Tuple[BookLevel, ...]
    asks: Tuple[BookLevel, ...]
    snapshot: bool = False

    def __post_init__(self) -> None:
        if not self.update_id.strip():
            raise ValidationError("book update id is required")
        venue = self.venue.strip().upper()
        symbol = pair(self.symbol)
        if not venue:
            raise ValidationError("book update venue is required")
        if self.sequence < 1:
            raise ValidationError("book update sequence must be positive")
        if any(level.venue != venue or level.symbol != symbol for level in self.bids + self.asks):
            raise ValidationError("book levels do not match update venue and symbol")
        if any(level.side != BookSide.BID for level in self.bids):
            raise ValidationError("bid update contains an ask")
        if any(level.side != BookSide.ASK for level in self.asks):
            raise ValidationError("ask update contains a bid")


@dataclass(frozen=True)
class BookGap:
    venue: str
    symbol: str
    expected_sequence: int
    received_sequence: int
    detected_at: datetime


@dataclass(frozen=True)
class SweepResult:
    symbol: str
    side: str
    requested_quantity: Decimal
    filled_quantity: Decimal
    average_price: Optional[Decimal]
    notional: Decimal
    levels: Tuple[Tuple[str, Decimal, Decimal], ...]
    complete: bool


class ConsolidatedOrderBook:
    def __init__(self, *, stale_after: timedelta = timedelta(seconds=3)) -> None:
        if stale_after <= timedelta(0):
            raise ValidationError("stale threshold must be positive")
        self._stale_after = stale_after
        self._books: Dict[Tuple[str, str], Dict[BookSide, Dict[Decimal, BookLevel]]] = {}
        self._sequences: Dict[Tuple[str, str], int] = {}
        self._observed_at: Dict[Tuple[str, str], datetime] = {}
        self._updates: Dict[str, VenueBookUpdate] = {}
        self._gaps: list[BookGap] = []
        self._snapshot_sequence = 0
        self._lock = RLock()

    def apply(self, update: VenueBookUpdate) -> bool:
        if not isinstance(update, VenueBookUpdate):
            raise ValidationError("update must be VenueBookUpdate")
        key = (update.venue.upper(), pair(update.symbol))
        with self._lock:
            existing = self._updates.get(update.update_id)
            if existing is not None:
                if existing == update:
                    return False
                raise StorageConflictError("book update id already exists")
            previous = self._sequences.get(key)
            if previous is not None:
                if update.sequence <= previous:
                    raise StorageConflictError("book sequence moved backward")
                if not update.snapshot and update.sequence != previous + 1:
                    self._gaps.append(
                        BookGap(
                            venue=key[0],
                            symbol=key[1],
                            expected_sequence=previous + 1,
                            received_sequence=update.sequence,
                            detected_at=update.observed_at,
                        )
                    )
                    raise StorageConflictError("book sequence contains a gap")
            if update.snapshot or key not in self._books:
                state = {BookSide.BID: {}, BookSide.ASK: {}}
            else:
                current = self._books[key]
                state = {
                    BookSide.BID: dict(current[BookSide.BID]),
                    BookSide.ASK: dict(current[BookSide.ASK]),
                }
            self._apply_levels(state, update.bids)
            self._apply_levels(state, update.asks)
            bids = state[BookSide.BID]
            asks = state[BookSide.ASK]
            if bids and asks and max(bids) >= min(asks):
                raise ValidationError("book update creates a crossed market")
            self._books[key] = state
            self._sequences[key] = update.sequence
            self._observed_at[key] = update.observed_at
            self._updates[update.update_id] = update
            return True

    def snapshot(
        self,
        symbol: str,
        *,
        at: datetime,
        depth: Optional[int] = None,
        include_stale: bool = False,
        firm_only: bool = True,
    ) -> MarketSnapshot:
        symbol_value = pair(symbol)
        instant = timestamp(at, "snapshot time")
        if depth is not None and depth < 1:
            raise ValidationError("book depth must be positive")
        with self._lock:
            bids = []
            asks = []
            stale = []
            for (venue, book_symbol), sides in self._books.items():
                if book_symbol != symbol_value:
                    continue
                observed = self._observed_at[(venue, book_symbol)]
                is_stale = instant - observed > self._stale_after
                if is_stale:
                    stale.append(venue)
                    if not include_stale:
                        continue
                bids.extend(
                    level
                    for level in sides[BookSide.BID].values()
                    if not firm_only or level.firm
                )
                asks.extend(
                    level
                    for level in sides[BookSide.ASK].values()
                    if not firm_only or level.firm
                )
            bids.sort(key=lambda level: (-level.price, level.venue, level.source_sequence))
            asks.sort(key=lambda level: (level.price, level.venue, level.source_sequence))
            if depth is not None:
                bids = bids[:depth]
                asks = asks[:depth]
            self._snapshot_sequence += 1
            return MarketSnapshot(
                symbol=symbol_value,
                sequence=self._snapshot_sequence,
                bids=tuple(bids),
                asks=tuple(asks),
                observed_at=instant,
                stale_venues=tuple(stale),
            )

    def sweep(
        self,
        symbol: str,
        side: str,
        quantity: object,
        *,
        at: datetime,
        limit_price: Optional[object] = None,
        allow_partial: bool = False,
    ) -> SweepResult:
        requested = Decimal(str(quantity))
        if requested <= 0:
            raise ValidationError("sweep quantity must be positive")
        direction = text(side, "side").lower()
        if direction not in {"buy", "sell"}:
            raise ValidationError("sweep side must be buy or sell")
        limit = Decimal(str(limit_price)) if limit_price is not None else None
        snapshot = self.snapshot(symbol, at=at)
        levels = snapshot.asks if direction == "buy" else snapshot.bids
        remaining = requested
        notional = Decimal("0")
        fills = []
        for level in levels:
            if limit is not None:
                outside = level.price > limit if direction == "buy" else level.price < limit
                if outside:
                    break
            available = level.quantity
            if remaining < level.minimum_quantity:
                continue
            fill = min(remaining, available)
            if fill < level.minimum_quantity:
                continue
            fills.append((level.venue, level.price, fill))
            notional += fill * level.price
            remaining -= fill
            if remaining == 0:
                break
        filled = requested - remaining
        complete = remaining == 0
        if not complete and not allow_partial:
            return SweepResult(snapshot.symbol, direction, requested, Decimal("0"), None, Decimal("0"), (), False)
        average = notional / filled if filled else None
        return SweepResult(snapshot.symbol, direction, requested, filled, average, notional, tuple(fills), complete)

    def depth(
        self,
        symbol: str,
        side: BookSide,
        *,
        at: datetime,
        within_pips: Optional[object] = None,
        pip_size: object = "0.0001",
    ) -> Decimal:
        snapshot = self.snapshot(symbol, at=at)
        levels = snapshot.bids if BookSide(side) == BookSide.BID else snapshot.asks
        if not levels:
            return Decimal("0")
        if within_pips is None:
            return sum((level.quantity for level in levels), Decimal("0"))
        boundary = Decimal(str(within_pips)) * Decimal(str(pip_size))
        best = levels[0].price
        return sum(
            (
                level.quantity
                for level in levels
                if abs(level.price - best) <= boundary
            ),
            Decimal("0"),
        )

    def venue_sequences(self, symbol: str) -> Mapping[str, int]:
        symbol_value = pair(symbol)
        with self._lock:
            return {
                venue: sequence
                for (venue, book_symbol), sequence in self._sequences.items()
                if book_symbol == symbol_value
            }

    def gaps(self) -> Tuple[BookGap, ...]:
        with self._lock:
            return tuple(self._gaps)

    def clear_venue(self, venue: str, symbol: str) -> None:
        key = (text(venue, "venue").upper(), pair(symbol))
        with self._lock:
            self._books.pop(key, None)
            self._sequences.pop(key, None)
            self._observed_at.pop(key, None)

    @staticmethod
    def _apply_levels(
        state: Dict[BookSide, Dict[Decimal, BookLevel]],
        levels: Iterable[BookLevel],
    ) -> None:
        for level in levels:
            side = state[level.side]
            if level.quantity == 0:
                side.pop(level.price, None)
            else:
                side[level.price] = level
