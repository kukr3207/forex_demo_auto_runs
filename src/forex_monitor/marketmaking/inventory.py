"""Dealer inventory, cost basis, limits, and mark-to-market accounting."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from threading import RLock
from typing import Dict, Iterable, Mapping, Optional, Tuple

from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError
from forex_monitor.marketmaking.models import (
    DealerFill,
    InventoryPosition,
    number,
    text,
    timestamp,
)


@dataclass(frozen=True)
class InventoryLimit:
    currency: str
    soft_limit: Decimal
    hard_limit: Decimal
    target: Decimal = Decimal("0")
    enabled: bool = True

    def __post_init__(self) -> None:
        code = text(self.currency, "currency").upper()
        if len(code) != 3 or not code.isalpha():
            raise ValidationError("inventory limit currency is invalid")
        soft = number(self.soft_limit, "soft limit", positive=True)
        hard = number(self.hard_limit, "hard limit", positive=True)
        target = number(self.target, "inventory target")
        if soft > hard:
            raise ValidationError("soft limit cannot exceed hard limit")
        if abs(target) > hard:
            raise ValidationError("inventory target exceeds hard limit")
        object.__setattr__(self, "currency", code)
        object.__setattr__(self, "soft_limit", soft)
        object.__setattr__(self, "hard_limit", hard)
        object.__setattr__(self, "target", target)


@dataclass(frozen=True)
class InventoryTransaction:
    transaction_id: str
    currency: str
    quantity: Decimal
    unit_cost: Decimal
    source: str
    occurred_at: datetime
    realized_pnl: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        object.__setattr__(self, "transaction_id", text(self.transaction_id, "transaction id"))
        code = text(self.currency, "currency").upper()
        if len(code) != 3 or not code.isalpha():
            raise ValidationError("inventory transaction currency is invalid")
        object.__setattr__(self, "currency", code)
        quantity = number(self.quantity, "transaction quantity")
        if quantity == 0:
            raise ValidationError("inventory transaction quantity cannot be zero")
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "unit_cost", number(self.unit_cost, "unit cost", non_negative=True))
        object.__setattr__(self, "source", text(self.source, "transaction source"))
        object.__setattr__(self, "occurred_at", timestamp(self.occurred_at, "occurred at"))
        object.__setattr__(self, "realized_pnl", number(self.realized_pnl, "realized PnL"))


@dataclass(frozen=True)
class LimitBreach:
    currency: str
    severity: str
    quantity: Decimal
    limit: Decimal
    excess: Decimal
    detected_at: datetime


@dataclass(frozen=True)
class InventoryValuation:
    currency: str
    quantity: Decimal
    mark: Decimal
    market_value: Decimal
    unrealized_pnl: Decimal
    realized_pnl: Decimal
    funding_pnl: Decimal

    @property
    def total_pnl(self) -> Decimal:
        return self.unrealized_pnl + self.realized_pnl + self.funding_pnl


class InventoryManager:
    """Maintain currency inventory with average-cost accounting.

    A client buy means the dealer sells base currency and receives quote
    currency. Both legs are booked atomically and a repeated fill is harmless.
    """

    def __init__(self, *, reporting_currency: str = "USD") -> None:
        code = text(reporting_currency, "reporting currency").upper()
        if len(code) != 3 or not code.isalpha():
            raise ValidationError("reporting currency is invalid")
        self.reporting_currency = code
        self._positions: Dict[str, InventoryPosition] = {}
        self._limits: Dict[str, InventoryLimit] = {}
        self._transactions: Dict[str, InventoryTransaction] = {}
        self._fill_fingerprints: Dict[str, Tuple[object, ...]] = {}
        self._lock = RLock()

    def set_limit(self, limit: InventoryLimit) -> InventoryLimit:
        if not isinstance(limit, InventoryLimit):
            raise ValidationError("limit must be InventoryLimit")
        with self._lock:
            self._limits[limit.currency] = limit
            return limit

    def apply_fill(self, fill: DealerFill) -> Tuple[InventoryTransaction, InventoryTransaction]:
        if not isinstance(fill, DealerFill):
            raise ValidationError("fill must be DealerFill")
        fingerprint = (
            fill.quote_id,
            fill.symbol,
            fill.side,
            fill.quantity,
            fill.price,
            fill.filled_at,
            fill.client_id,
        )
        base = fill.symbol[:3]
        quote = fill.symbol[3:]
        base_change = -fill.quantity if fill.side == "buy" else fill.quantity
        quote_change = fill.quantity * fill.price if fill.side == "buy" else -(fill.quantity * fill.price)
        base_tx = InventoryTransaction(
            transaction_id="%s:base" % fill.fill_id,
            currency=base,
            quantity=base_change,
            unit_cost=fill.price,
            source="dealer-fill:%s" % fill.fill_id,
            occurred_at=fill.filled_at,
        )
        quote_tx = InventoryTransaction(
            transaction_id="%s:quote" % fill.fill_id,
            currency=quote,
            quantity=quote_change,
            unit_cost=Decimal("1"),
            source="dealer-fill:%s" % fill.fill_id,
            occurred_at=fill.filled_at,
        )
        with self._lock:
            existing = self._fill_fingerprints.get(fill.fill_id)
            if existing is not None:
                if existing != fingerprint:
                    raise StorageConflictError("inventory fill id already has different terms")
                return self._transactions[base_tx.transaction_id], self._transactions[quote_tx.transaction_id]
            next_base, stored_base = self._preview_transaction(base_tx)
            next_quote, stored_quote = self._preview_transaction(quote_tx)
            self._enforce_hard_limit(next_base)
            self._enforce_hard_limit(next_quote)
            self._positions[base] = next_base
            self._positions[quote] = next_quote
            self._transactions[stored_base.transaction_id] = stored_base
            self._transactions[stored_quote.transaction_id] = stored_quote
            self._fill_fingerprints[fill.fill_id] = fingerprint
            return stored_base, stored_quote

    def apply_adjustment(self, transaction: InventoryTransaction) -> InventoryTransaction:
        if not isinstance(transaction, InventoryTransaction):
            raise ValidationError("transaction must be InventoryTransaction")
        with self._lock:
            existing = self._transactions.get(transaction.transaction_id)
            if existing is not None:
                if existing.currency == transaction.currency and existing.quantity == transaction.quantity and existing.unit_cost == transaction.unit_cost:
                    return existing
                raise StorageConflictError("inventory transaction id already exists")
            position, stored = self._preview_transaction(transaction)
            self._enforce_hard_limit(position)
            self._positions[position.currency] = position
            self._transactions[stored.transaction_id] = stored
            return stored

    def accrue_funding(self, currency: str, amount: object, *, at: datetime) -> InventoryPosition:
        code = text(currency, "currency").upper()
        value = number(amount, "funding amount")
        instant = timestamp(at, "funding time")
        with self._lock:
            current = self._positions.get(code, self._empty_position(code, instant))
            changed = replace(
                current,
                funding_pnl=current.funding_pnl + value,
                updated_at=instant,
                version=current.version + 1,
            )
            self._positions[code] = changed
            return changed

    def position(self, currency: str) -> InventoryPosition:
        code = text(currency, "currency").upper()
        with self._lock:
            value = self._positions.get(code)
            if value is None:
                raise NotFoundError("inventory position does not exist")
            return value

    def positions(self, *, include_flat: bool = False) -> Tuple[InventoryPosition, ...]:
        with self._lock:
            values = [item for item in self._positions.values() if include_flat or item.quantity != 0]
            return tuple(sorted(values, key=lambda item: item.currency))

    def transactions(self, currency: Optional[str] = None) -> Tuple[InventoryTransaction, ...]:
        code = currency.upper() if currency else None
        with self._lock:
            values = [item for item in self._transactions.values() if code is None or item.currency == code]
            return tuple(sorted(values, key=lambda item: (item.occurred_at, item.transaction_id)))

    def limit_breaches(self, *, at: datetime) -> Tuple[LimitBreach, ...]:
        instant = timestamp(at, "breach time")
        result = []
        with self._lock:
            for currency, limit in self._limits.items():
                if not limit.enabled:
                    continue
                quantity = self._positions.get(currency, self._empty_position(currency, instant)).quantity
                absolute = abs(quantity)
                if absolute > limit.hard_limit:
                    result.append(LimitBreach(currency, "hard", quantity, limit.hard_limit, absolute - limit.hard_limit, instant))
                elif absolute > limit.soft_limit:
                    result.append(LimitBreach(currency, "soft", quantity, limit.soft_limit, absolute - limit.soft_limit, instant))
        return tuple(sorted(result, key=lambda item: (item.severity != "hard", item.currency)))

    def valuations(self, marks: Mapping[str, object]) -> Tuple[InventoryValuation, ...]:
        result = []
        with self._lock:
            for position in self._positions.values():
                if position.currency not in marks:
                    raise ValidationError("missing inventory mark for %s" % position.currency)
                mark = number(marks[position.currency], "inventory mark", non_negative=True)
                market_value = position.quantity * mark
                book_value = position.quantity * position.average_cost
                result.append(
                    InventoryValuation(
                        currency=position.currency,
                        quantity=position.quantity,
                        mark=mark,
                        market_value=market_value,
                        unrealized_pnl=market_value - book_value,
                        realized_pnl=position.realized_pnl,
                        funding_pnl=position.funding_pnl,
                    )
                )
        return tuple(sorted(result, key=lambda item: item.currency))

    def inventory_ratio(self, currency: str) -> Decimal:
        code = text(currency, "currency").upper()
        with self._lock:
            limit = self._limits.get(code)
            if limit is None or not limit.enabled:
                return Decimal("0")
            quantity = self._positions.get(code)
            if quantity is None:
                return Decimal("0")
            ratio = (quantity.quantity - limit.target) / limit.hard_limit
            return min(Decimal("1"), max(Decimal("-1"), ratio))

    def _preview_transaction(self, transaction: InventoryTransaction) -> Tuple[InventoryPosition, InventoryTransaction]:
        current = self._positions.get(transaction.currency, self._empty_position(transaction.currency, transaction.occurred_at))
        old_quantity = current.quantity
        change = transaction.quantity
        new_quantity = old_quantity + change
        realized = Decimal("0")
        if old_quantity == 0 or old_quantity * change > 0:
            notional = abs(old_quantity) * current.average_cost + abs(change) * transaction.unit_cost
            average = notional / abs(new_quantity) if new_quantity else Decimal("0")
        elif abs(change) <= abs(old_quantity):
            closed = abs(change)
            direction = Decimal("1") if old_quantity > 0 else Decimal("-1")
            realized = closed * (transaction.unit_cost - current.average_cost) * direction
            average = current.average_cost if new_quantity else Decimal("0")
        else:
            closed = abs(old_quantity)
            direction = Decimal("1") if old_quantity > 0 else Decimal("-1")
            realized = closed * (transaction.unit_cost - current.average_cost) * direction
            average = transaction.unit_cost
        stored = replace(transaction, realized_pnl=realized)
        position = InventoryPosition(
            currency=current.currency,
            quantity=new_quantity,
            average_cost=average,
            realized_pnl=current.realized_pnl + realized,
            funding_pnl=current.funding_pnl,
            updated_at=transaction.occurred_at,
            version=current.version + 1,
        )
        return position, stored

    def _enforce_hard_limit(self, position: InventoryPosition) -> None:
        limit = self._limits.get(position.currency)
        if limit is not None and limit.enabled and abs(position.quantity) > limit.hard_limit:
            raise ValidationError("inventory hard limit exceeded for %s" % position.currency)

    @staticmethod
    def _empty_position(currency: str, at: datetime) -> InventoryPosition:
        return InventoryPosition(currency, Decimal("0"), Decimal("0"), updated_at=at, version=0)
