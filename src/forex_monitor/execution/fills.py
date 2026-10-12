"""Execution fills with idempotent booking and position summaries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Dict, Mapping, Optional, Tuple

from forex_monitor.errors import StorageConflictError, ValidationError
from forex_monitor.execution.orders import OrderSide
from forex_monitor.models import as_decimal, ensure_utc, normalize_symbol


@dataclass(frozen=True)
class Fill:
    fill_id: str
    order_id: str
    symbol: str
    side: OrderSide
    quantity: Decimal
    price: Decimal
    executed_at: datetime
    venue_id: str
    liquidity: str = "unknown"
    commission: Decimal = Decimal("0")
    commission_currency: Optional[str] = None
    external_id: Optional[str] = None

    def __post_init__(self) -> None:
        fill_id = self.fill_id.strip()
        order_id = self.order_id.strip()
        venue = self.venue_id.strip()
        if not fill_id or not order_id or not venue:
            raise ValidationError("fill, order, and venue ids cannot be blank")
        side = self.side if isinstance(self.side, OrderSide) else OrderSide(self.side)
        commission = as_decimal(self.commission, "commission")
        if commission < 0:
            raise ValidationError("commission cannot be negative")
        liquidity = self.liquidity.strip().lower()
        if liquidity not in {"maker", "taker", "unknown"}:
            raise ValidationError("liquidity must be maker, taker, or unknown")
        object.__setattr__(self, "fill_id", fill_id)
        object.__setattr__(self, "order_id", order_id)
        object.__setattr__(self, "venue_id", venue)
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "quantity", as_decimal(self.quantity, "fill quantity", positive=True))
        object.__setattr__(self, "price", as_decimal(self.price, "fill price", positive=True))
        object.__setattr__(self, "commission", commission)
        object.__setattr__(self, "executed_at", ensure_utc(self.executed_at, "execution time"))
        object.__setattr__(self, "liquidity", liquidity)

    @property
    def signed_quantity(self) -> Decimal:
        return self.side.sign * self.quantity

    @property
    def notional(self) -> Decimal:
        return self.quantity * self.price


@dataclass(frozen=True)
class FillSummary:
    symbol: str
    quantity: Decimal
    gross_bought: Decimal
    gross_sold: Decimal
    average_buy_price: Optional[Decimal]
    average_sell_price: Optional[Decimal]
    commissions: Decimal
    fill_count: int


class FillLedger:
    def __init__(self) -> None:
        self._fills: Dict[str, Fill] = {}
        self._external: Dict[Tuple[str, str], str] = {}

    def book(self, fill: Fill) -> Fill:
        existing = self._fills.get(fill.fill_id)
        if existing is not None:
            if existing == fill:
                return existing
            raise StorageConflictError("fill id was reused with different data")
        if fill.external_id:
            key = (fill.venue_id, fill.external_id)
            existing_id = self._external.get(key)
            if existing_id:
                existing = self._fills[existing_id]
                if existing == fill:
                    return existing
                raise StorageConflictError("external fill id was reused with different data")
            self._external[key] = fill.fill_id
        self._fills[fill.fill_id] = fill
        return fill

    def for_order(self, order_id: str) -> Tuple[Fill, ...]:
        return tuple(sorted(
            (fill for fill in self._fills.values() if fill.order_id == order_id),
            key=lambda item: (item.executed_at, item.fill_id),
        ))

    def for_symbol(self, symbol: str) -> Tuple[Fill, ...]:
        normalized = normalize_symbol(symbol)
        return tuple(sorted(
            (fill for fill in self._fills.values() if fill.symbol == normalized),
            key=lambda item: (item.executed_at, item.fill_id),
        ))

    def summary(self, symbol: str) -> FillSummary:
        fills = self.for_symbol(symbol)
        buys = [fill for fill in fills if fill.side is OrderSide.BUY]
        sells = [fill for fill in fills if fill.side is OrderSide.SELL]
        bought = sum((fill.quantity for fill in buys), Decimal("0"))
        sold = sum((fill.quantity for fill in sells), Decimal("0"))
        buy_notional = sum((fill.notional for fill in buys), Decimal("0"))
        sell_notional = sum((fill.notional for fill in sells), Decimal("0"))
        return FillSummary(
            normalize_symbol(symbol),
            bought - sold,
            bought,
            sold,
            buy_notional / bought if bought else None,
            sell_notional / sold if sold else None,
            sum((fill.commission for fill in fills), Decimal("0")),
            len(fills),
        )

    def all(self) -> Tuple[Fill, ...]:
        return tuple(sorted(self._fills.values(), key=lambda item: (item.executed_at, item.fill_id)))
