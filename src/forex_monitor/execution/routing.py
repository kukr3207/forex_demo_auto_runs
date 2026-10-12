"""Smart order routing using executable cost and available liquidity."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import List, Mapping, Optional, Tuple

from forex_monitor.errors import ValidationError
from forex_monitor.execution.orders import Order, OrderSide, OrderType
from forex_monitor.execution.venues import VenueBook
from forex_monitor.models import as_decimal, ensure_utc


@dataclass(frozen=True)
class RouteLeg:
    venue_id: str
    quantity: Decimal
    expected_price: Decimal
    effective_price: Decimal
    quote_id: Optional[str]

    @property
    def expected_notional(self) -> Decimal:
        return self.quantity * self.expected_price


@dataclass(frozen=True)
class RouteDecision:
    order_id: str
    decided_at: datetime
    requested_quantity: Decimal
    routed_quantity: Decimal
    legs: Tuple[RouteLeg, ...]
    complete: bool
    reason: Optional[str] = None

    @property
    def average_price(self) -> Optional[Decimal]:
        if self.routed_quantity == 0:
            return None
        return sum((leg.expected_notional for leg in self.legs), Decimal("0")) / self.routed_quantity

    def as_dict(self) -> Mapping[str, object]:
        return {
            "orderId": self.order_id,
            "decidedAt": self.decided_at.isoformat(),
            "requestedQuantity": str(self.requested_quantity),
            "routedQuantity": str(self.routed_quantity),
            "complete": self.complete,
            "averagePrice": str(self.average_price) if self.average_price is not None else None,
            "reason": self.reason,
            "legs": tuple(
                {
                    "venueId": leg.venue_id,
                    "quantity": str(leg.quantity),
                    "expectedPrice": str(leg.expected_price),
                    "effectivePrice": str(leg.effective_price),
                    "quoteId": leg.quote_id,
                }
                for leg in self.legs
            ),
        }


class SmartOrderRouter:
    def __init__(
        self,
        venues: VenueBook,
        *,
        maximum_quote_age: timedelta = timedelta(seconds=5),
        allow_partial: bool = True,
    ) -> None:
        if maximum_quote_age.total_seconds() < 0:
            raise ValidationError("maximum quote age cannot be negative")
        self.venues = venues
        self.maximum_quote_age = maximum_quote_age
        self.allow_partial = allow_partial

    def route(self, order: Order, now: datetime) -> RouteDecision:
        timestamp = ensure_utc(now, "routing time")
        if order.remaining_quantity <= 0:
            raise ValidationError("order has no remaining quantity")
        candidates = self.venues.executable(
            order.symbol,
            order.remaining_quantity,
            timestamp,
            self.maximum_quote_age,
        )
        ranked = []
        for venue, quote in candidates:
            buy = order.side is OrderSide.BUY
            price = quote.ask if buy else quote.bid
            available = quote.ask_size if buy else quote.bid_size
            effective = venue.effective_price(price, taker=True, buy=buy)
            ranked.append((effective, venue.priority, venue.venue_id, venue, quote, price, available))
        ranked.sort(key=lambda item: (item[0], item[1], item[2]), reverse=order.side is OrderSide.SELL)
        remaining = order.remaining_quantity
        legs: List[RouteLeg] = []
        for _, _, _, venue, quote, price, available in ranked:
            quantity = min(remaining, available)
            if quantity < venue.minimum_quantity:
                continue
            if venue.maximum_quantity is not None:
                quantity = min(quantity, venue.maximum_quantity)
            if order.order_type in {OrderType.LIMIT, OrderType.STOP_LIMIT}:
                if order.side is OrderSide.BUY and price > order.limit_price:
                    continue
                if order.side is OrderSide.SELL and price < order.limit_price:
                    continue
            effective = venue.effective_price(price, taker=True, buy=order.side is OrderSide.BUY)
            legs.append(RouteLeg(venue.venue_id, quantity, price, effective, quote.quote_id))
            remaining -= quantity
            if remaining == 0:
                break
        routed = order.remaining_quantity - remaining
        complete = remaining == 0
        if not complete and not self.allow_partial:
            return RouteDecision(order.order_id, timestamp, order.remaining_quantity, Decimal("0"), (), False, "insufficient liquidity")
        reason = None if complete else "partial liquidity"
        if routed == 0:
            reason = "no executable venue"
        return RouteDecision(order.order_id, timestamp, order.remaining_quantity, routed, tuple(legs), complete, reason)

    def estimated_cost(self, decision: RouteDecision, benchmark: object, side: OrderSide) -> Decimal:
        reference = as_decimal(benchmark, "routing benchmark", positive=True)
        if decision.routed_quantity == 0:
            return Decimal("0")
        signed = Decimal("1") if side is OrderSide.BUY else Decimal("-1")
        cost = sum(
            (signed * (leg.effective_price - reference) * leg.quantity for leg in decision.legs),
            Decimal("0"),
        )
        return cost
