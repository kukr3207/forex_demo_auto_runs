"""Order lifecycle with explicit transition and quantity invariants."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Dict, Iterable, Mapping, Optional, Tuple

from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError
from forex_monitor.models import as_decimal, ensure_utc, normalize_symbol, utc_now


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> Decimal:
        return Decimal("1") if self is OrderSide.BUY else Decimal("-1")


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"
    PEGGED = "pegged"


class TimeInForce(str, Enum):
    DAY = "day"
    GOOD_TILL_CANCEL = "good_till_cancel"
    IMMEDIATE_OR_CANCEL = "immediate_or_cancel"
    FILL_OR_KILL = "fill_or_kill"
    GOOD_TILL_TIME = "good_till_time"


class OrderState(str, Enum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    ROUTED = "routed"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCEL_PENDING = "cancel_pending"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"

    @property
    def terminal(self) -> bool:
        return self in {
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
        }


ALLOWED_TRANSITIONS = {
    OrderState.DRAFT: {OrderState.PENDING_APPROVAL, OrderState.APPROVED, OrderState.CANCELLED},
    OrderState.PENDING_APPROVAL: {OrderState.APPROVED, OrderState.REJECTED, OrderState.CANCELLED},
    OrderState.APPROVED: {OrderState.ROUTED, OrderState.CANCELLED, OrderState.EXPIRED},
    OrderState.ROUTED: {
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
        OrderState.CANCEL_PENDING,
        OrderState.REJECTED,
        OrderState.EXPIRED,
    },
    OrderState.PARTIALLY_FILLED: {
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
        OrderState.CANCEL_PENDING,
        OrderState.EXPIRED,
    },
    OrderState.CANCEL_PENDING: {OrderState.CANCELLED, OrderState.PARTIALLY_FILLED, OrderState.FILLED},
}


@dataclass(frozen=True)
class Order:
    order_id: str
    symbol: str
    side: OrderSide
    quantity: Decimal
    order_type: OrderType = OrderType.MARKET
    time_in_force: TimeInForce = TimeInForce.DAY
    limit_price: Optional[Decimal] = None
    stop_price: Optional[Decimal] = None
    expires_at: Optional[datetime] = None
    state: OrderState = OrderState.DRAFT
    filled_quantity: Decimal = Decimal("0")
    average_price: Optional[Decimal] = None
    account_id: Optional[str] = None
    strategy_id: Optional[str] = None
    client_order_id: Optional[str] = None
    parent_order_id: Optional[str] = None
    version: int = 1
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        identifier = self.order_id.strip()
        if not identifier:
            raise ValidationError("order id cannot be blank")
        side = self.side if isinstance(self.side, OrderSide) else OrderSide(self.side)
        order_type = self.order_type if isinstance(self.order_type, OrderType) else OrderType(self.order_type)
        tif = self.time_in_force if isinstance(self.time_in_force, TimeInForce) else TimeInForce(self.time_in_force)
        state = self.state if isinstance(self.state, OrderState) else OrderState(self.state)
        quantity = as_decimal(self.quantity, "order quantity", positive=True)
        filled = as_decimal(self.filled_quantity, "filled quantity")
        if filled < 0 or filled > quantity:
            raise ValidationError("filled quantity must be between zero and order quantity")
        limit_price = as_decimal(self.limit_price, "limit price", positive=True) if self.limit_price is not None else None
        stop_price = as_decimal(self.stop_price, "stop price", positive=True) if self.stop_price is not None else None
        if order_type in {OrderType.LIMIT, OrderType.STOP_LIMIT} and limit_price is None:
            raise ValidationError("limit orders require a limit price")
        if order_type in {OrderType.STOP, OrderType.STOP_LIMIT} and stop_price is None:
            raise ValidationError("stop orders require a stop price")
        if tif is TimeInForce.GOOD_TILL_TIME and self.expires_at is None:
            raise ValidationError("good-till-time orders require an expiry")
        expiry = ensure_utc(self.expires_at, "order expiry") if self.expires_at else None
        created = ensure_utc(self.created_at, "created at")
        updated = ensure_utc(self.updated_at, "updated at")
        if updated < created:
            raise ValidationError("order update time cannot precede creation")
        if expiry is not None and expiry <= created:
            raise ValidationError("order expiry must follow creation")
        if self.version <= 0:
            raise ValidationError("order version must be positive")
        average = as_decimal(self.average_price, "average price", positive=True) if self.average_price is not None else None
        if filled > 0 and average is None:
            raise ValidationError("filled orders require an average price")
        if filled == 0 and average is not None:
            raise ValidationError("unfilled orders cannot have an average price")
        if state is OrderState.FILLED and filled != quantity:
            raise ValidationError("filled state requires the full order quantity")
        object.__setattr__(self, "order_id", identifier)
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "order_type", order_type)
        object.__setattr__(self, "time_in_force", tif)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "filled_quantity", filled)
        object.__setattr__(self, "limit_price", limit_price)
        object.__setattr__(self, "stop_price", stop_price)
        object.__setattr__(self, "average_price", average)
        object.__setattr__(self, "expires_at", expiry)
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "updated_at", updated)

    @property
    def remaining_quantity(self) -> Decimal:
        return self.quantity - self.filled_quantity

    @property
    def notional(self) -> Decimal:
        return self.filled_quantity * (self.average_price or Decimal("0"))

    def transition(self, state: OrderState, at: datetime, *, expected_version: Optional[int] = None) -> "Order":
        target = state if isinstance(state, OrderState) else OrderState(state)
        if expected_version is not None and expected_version != self.version:
            raise StorageConflictError("order version changed")
        if target not in ALLOWED_TRANSITIONS.get(self.state, set()):
            raise ValidationError("order state transition is not allowed")
        timestamp = ensure_utc(at, "transition time")
        if timestamp < self.updated_at:
            raise ValidationError("order transition time cannot move backward")
        return replace(self, state=target, version=self.version + 1, updated_at=timestamp)

    def apply_fill(self, quantity: object, price: object, at: datetime) -> "Order":
        if self.state not in {OrderState.ROUTED, OrderState.PARTIALLY_FILLED, OrderState.CANCEL_PENDING}:
            raise ValidationError("order is not eligible for fills")
        fill_quantity = as_decimal(quantity, "fill quantity", positive=True)
        fill_price = as_decimal(price, "fill price", positive=True)
        if fill_quantity > self.remaining_quantity:
            raise ValidationError("fill quantity exceeds the order remainder")
        new_quantity = self.filled_quantity + fill_quantity
        prior_notional = self.filled_quantity * (self.average_price or Decimal("0"))
        average = (prior_notional + fill_quantity * fill_price) / new_quantity
        state = OrderState.FILLED if new_quantity == self.quantity else OrderState.PARTIALLY_FILLED
        timestamp = ensure_utc(at, "fill time")
        if timestamp < self.updated_at:
            raise ValidationError("fill time cannot precede the last order update")
        return replace(
            self,
            filled_quantity=new_quantity,
            average_price=average,
            state=state,
            version=self.version + 1,
            updated_at=timestamp,
        )

    def as_dict(self) -> Mapping[str, object]:
        return {
            "orderId": self.order_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "quantity": str(self.quantity),
            "filledQuantity": str(self.filled_quantity),
            "remainingQuantity": str(self.remaining_quantity),
            "averagePrice": str(self.average_price) if self.average_price is not None else None,
            "orderType": self.order_type.value,
            "timeInForce": self.time_in_force.value,
            "state": self.state.value,
            "version": self.version,
            "createdAt": self.created_at.isoformat(),
            "updatedAt": self.updated_at.isoformat(),
        }


class OrderBook:
    def __init__(self) -> None:
        self._orders: Dict[str, Order] = {}
        self._client_ids: Dict[str, str] = {}

    def add(self, order: Order) -> Order:
        if order.order_id in self._orders:
            existing = self._orders[order.order_id]
            if existing == order:
                return existing
            raise StorageConflictError("order id already exists")
        if order.client_order_id:
            existing_id = self._client_ids.get(order.client_order_id)
            if existing_id:
                existing = self._orders[existing_id]
                if existing == order:
                    return existing
                raise StorageConflictError("client order id already exists")
            self._client_ids[order.client_order_id] = order.order_id
        self._orders[order.order_id] = order
        return order

    def get(self, order_id: str) -> Order:
        try:
            return self._orders[order_id]
        except KeyError as exc:
            raise NotFoundError("order was not found") from exc

    def save(self, order: Order, *, expected_version: int) -> Order:
        existing = self.get(order.order_id)
        if existing.version != expected_version:
            raise StorageConflictError("order version changed")
        if order.version <= existing.version:
            raise StorageConflictError("replacement order version did not advance")
        self._orders[order.order_id] = order
        return order

    def active(self, *, symbol: Optional[str] = None, account_id: Optional[str] = None) -> Tuple[Order, ...]:
        normalized = normalize_symbol(symbol) if symbol else None
        result = []
        for order in self._orders.values():
            if order.state.terminal:
                continue
            if normalized and order.symbol != normalized:
                continue
            if account_id and order.account_id != account_id:
                continue
            result.append(order)
        return tuple(sorted(result, key=lambda item: (item.created_at, item.order_id)))

    def expire(self, now: datetime) -> Tuple[Order, ...]:
        timestamp = ensure_utc(now, "expiry check time")
        expired = []
        for order in tuple(self._orders.values()):
            if order.state.terminal or order.expires_at is None or order.expires_at > timestamp:
                continue
            if OrderState.EXPIRED not in ALLOWED_TRANSITIONS.get(order.state, set()):
                continue
            changed = order.transition(OrderState.EXPIRED, timestamp)
            self._orders[order.order_id] = changed
            expired.append(changed)
        return tuple(expired)

    def all(self) -> Tuple[Order, ...]:
        return tuple(sorted(self._orders.values(), key=lambda item: (item.created_at, item.order_id)))
