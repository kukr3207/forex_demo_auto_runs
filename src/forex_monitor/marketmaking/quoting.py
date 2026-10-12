"""Client pricing with inventory skew and deterministic quote replay."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_EVEN
from threading import RLock
from typing import Dict, Mapping, Optional, Tuple

from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError
from forex_monitor.marketmaking.models import (
    DealerFill,
    DealerQuote,
    MarketSnapshot,
    QuoteState,
    number,
    pair,
    text,
    timestamp,
)


@dataclass(frozen=True)
class QuotePolicy:
    policy_id: str
    symbol: str
    base_spread_pips: Decimal
    minimum_spread_pips: Decimal
    maximum_spread_pips: Decimal
    size_breaks: Tuple[Tuple[Decimal, Decimal], ...]
    volatility_multiplier: Decimal = Decimal("1")
    inventory_skew_pips: Decimal = Decimal("0")
    client_markups: Tuple[Tuple[str, Decimal], ...] = ()
    quote_ttl_seconds: int = 15
    precision: int = 5
    enabled: bool = True
    version: int = 1

    def __post_init__(self) -> None:
        if not self.policy_id.strip():
            raise ValidationError("quote policy id is required")
        object.__setattr__(self, "symbol", pair(self.symbol))
        base = number(self.base_spread_pips, "base spread", non_negative=True)
        minimum = number(self.minimum_spread_pips, "minimum spread", non_negative=True)
        maximum = number(self.maximum_spread_pips, "maximum spread", positive=True)
        if minimum > base or base > maximum:
            raise ValidationError("quote policy spreads are inconsistent")
        object.__setattr__(self, "base_spread_pips", base)
        object.__setattr__(self, "minimum_spread_pips", minimum)
        object.__setattr__(self, "maximum_spread_pips", maximum)
        breaks = tuple(
            sorted(
                (
                    number(quantity, "size break", positive=True),
                    number(markup, "size markup", non_negative=True),
                )
                for quantity, markup in self.size_breaks
            )
        )
        if len({item[0] for item in breaks}) != len(breaks):
            raise ValidationError("quote policy contains duplicate size breaks")
        object.__setattr__(self, "size_breaks", breaks)
        object.__setattr__(self, "volatility_multiplier", number(self.volatility_multiplier, "volatility multiplier", non_negative=True))
        object.__setattr__(self, "inventory_skew_pips", number(self.inventory_skew_pips, "inventory skew"))
        object.__setattr__(self, "client_markups", tuple(sorted((key, Decimal(str(value))) for key, value in self.client_markups)))
        if self.quote_ttl_seconds < 1 or self.quote_ttl_seconds > 300:
            raise ValidationError("quote TTL must be between one and three hundred seconds")
        if self.precision < 1 or self.precision > 10:
            raise ValidationError("quote precision is invalid")


@dataclass(frozen=True)
class PricingContext:
    volatility_pips: Decimal = Decimal("0")
    inventory_ratio: Decimal = Decimal("0")
    liquidity_score: Decimal = Decimal("1")
    event_risk_pips: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if Decimal(str(self.volatility_pips)) < 0:
            raise ValidationError("volatility cannot be negative")
        if abs(Decimal(str(self.inventory_ratio))) > 1:
            raise ValidationError("inventory ratio must be between negative and positive one")
        score = Decimal(str(self.liquidity_score))
        if score <= 0 or score > 1:
            raise ValidationError("liquidity score must be in (0, 1]")
        if Decimal(str(self.event_risk_pips)) < 0:
            raise ValidationError("event risk cannot be negative")


class QuoteEngine:
    def __init__(self) -> None:
        self._policies: Dict[str, QuotePolicy] = {}
        self._quotes: Dict[str, DealerQuote] = {}
        self._request_index: Dict[str, str] = {}
        self._fills: Dict[str, DealerFill] = {}
        self._quote_fills: Dict[str, list[str]] = {}
        self._lock = RLock()

    def add_policy(self, policy: QuotePolicy) -> QuotePolicy:
        if not isinstance(policy, QuotePolicy):
            raise ValidationError("policy must be QuotePolicy")
        with self._lock:
            existing = self._policies.get(policy.policy_id)
            if existing is not None:
                if existing == policy:
                    return existing
                raise StorageConflictError("quote policy id already exists")
            if any(item.symbol == policy.symbol and item.enabled for item in self._policies.values()):
                raise StorageConflictError("symbol already has an enabled quote policy")
            self._policies[policy.policy_id] = policy
            return policy

    def update_policy(
        self,
        policy_id: str,
        *,
        expected_version: int,
        enabled: Optional[bool] = None,
        base_spread_pips: Optional[object] = None,
        quote_ttl_seconds: Optional[int] = None,
    ) -> QuotePolicy:
        with self._lock:
            current = self._require_policy(policy_id)
            if current.version != expected_version:
                raise StorageConflictError("quote policy version does not match")
            changes = {"version": current.version + 1}
            if enabled is not None:
                changes["enabled"] = enabled
            if base_spread_pips is not None:
                changes["base_spread_pips"] = Decimal(str(base_spread_pips))
            if quote_ttl_seconds is not None:
                changes["quote_ttl_seconds"] = quote_ttl_seconds
            changed = replace(current, **changes)
            self._policies[current.policy_id] = changed
            return changed

    def price(
        self,
        *,
        quote_id: str,
        request_id: str,
        client_id: str,
        symbol: str,
        side: str,
        quantity: object,
        market: MarketSnapshot,
        context: PricingContext,
        created_at: datetime,
        strategy: str = "default",
    ) -> DealerQuote:
        symbol_value = pair(symbol)
        direction = text(side, "side").lower()
        if direction not in {"buy", "sell"}:
            raise ValidationError("quote side must be buy or sell")
        size = number(quantity, "quantity", positive=True)
        instant = timestamp(created_at, "created at")
        if market.symbol != symbol_value:
            raise ValidationError("market snapshot does not match quote symbol")
        if market.midpoint is None:
            raise ValidationError("market snapshot is not two-sided")
        policy = self._policy_for(symbol_value)
        request_key = "%s:%s" % (client_id, request_id)
        spread = self._spread(policy, client_id, size, context)
        skew = Decimal(str(context.inventory_ratio)) * policy.inventory_skew_pips
        pip_size = Decimal("0.01") if symbol_value.endswith("JPY") else Decimal("0.0001")
        half_spread = spread * pip_size / Decimal("2")
        skew_amount = skew * pip_size
        reference = market.midpoint
        raw_price = (
            reference + half_spread - skew_amount
            if direction == "buy"
            else reference - half_spread - skew_amount
        )
        quantum = Decimal("1").scaleb(-policy.precision)
        price = raw_price.quantize(quantum, rounding=ROUND_HALF_EVEN)
        value = DealerQuote(
            quote_id=quote_id,
            request_id=request_id,
            client_id=client_id,
            symbol=symbol_value,
            side=direction,
            quantity=size,
            price=price,
            created_at=instant,
            expires_at=instant + timedelta(seconds=policy.quote_ttl_seconds),
            skew_pips=skew,
            spread_pips=spread,
            reference_price=reference,
            strategy=strategy,
        )
        with self._lock:
            existing_request = self._request_index.get(request_key)
            if existing_request:
                existing = self._quotes[existing_request]
                if self._quote_identity(existing) == self._quote_identity(value):
                    return existing
                raise StorageConflictError("quote request id was reused with different terms")
            if value.quote_id in self._quotes:
                raise StorageConflictError("quote id already exists")
            self._quotes[value.quote_id] = value
            self._request_index[request_key] = value.quote_id
            self._quote_fills[value.quote_id] = []
            return value

    def fill(
        self,
        quote_id: str,
        *,
        fill_id: str,
        quantity: object,
        filled_at: datetime,
    ) -> DealerFill:
        size = number(quantity, "fill quantity", positive=True)
        instant = timestamp(filled_at, "filled at")
        with self._lock:
            quote = self._require_quote(quote_id)
            existing = self._fills.get(fill_id)
            if existing is not None:
                if existing.quote_id == quote.quote_id and existing.quantity == size:
                    return existing
                raise StorageConflictError("fill id already exists")
            if quote.state not in {QuoteState.LIVE, QuoteState.FILLED}:
                raise ValidationError("quote is not fillable")
            if instant > quote.expires_at:
                self._quotes[quote.quote_id] = replace(
                    quote,
                    state=QuoteState.EXPIRED,
                    version=quote.version + 1,
                )
                raise ValidationError("quote has expired")
            if size > quote.remaining_quantity:
                raise ValidationError("fill exceeds quote remaining quantity")
            total = quote.filled_quantity + size
            state = QuoteState.FILLED if total == quote.quantity else QuoteState.LIVE
            fill = DealerFill(
                fill_id=fill_id,
                quote_id=quote.quote_id,
                symbol=quote.symbol,
                side=quote.side,
                quantity=size,
                price=quote.price,
                filled_at=instant,
                client_id=quote.client_id,
            )
            changed = replace(
                quote,
                filled_quantity=total,
                state=state,
                version=quote.version + 1,
            )
            self._quotes[quote.quote_id] = changed
            self._fills[fill.fill_id] = fill
            self._quote_fills[quote.quote_id].append(fill.fill_id)
            return fill

    def cancel(
        self,
        quote_id: str,
        *,
        expected_version: int,
    ) -> DealerQuote:
        with self._lock:
            current = self._require_quote(quote_id)
            if current.version != expected_version:
                raise StorageConflictError("quote version does not match")
            if current.state != QuoteState.LIVE:
                raise ValidationError("only live quotes can be cancelled")
            changed = replace(
                current,
                state=QuoteState.CANCELLED,
                version=current.version + 1,
            )
            self._quotes[current.quote_id] = changed
            return changed

    def expire(self, at: datetime) -> Tuple[DealerQuote, ...]:
        instant = timestamp(at, "expiry time")
        expired = []
        with self._lock:
            for quote_id, current in list(self._quotes.items()):
                if current.state == QuoteState.LIVE and current.expires_at < instant:
                    changed = replace(
                        current,
                        state=QuoteState.EXPIRED,
                        version=current.version + 1,
                    )
                    self._quotes[quote_id] = changed
                    expired.append(changed)
        return tuple(sorted(expired, key=lambda item: item.expires_at))

    def quote(self, quote_id: str) -> Optional[DealerQuote]:
        with self._lock:
            return self._quotes.get(quote_id.strip())

    def fills(self, quote_id: str) -> Tuple[DealerFill, ...]:
        with self._lock:
            return tuple(
                self._fills[item]
                for item in self._quote_fills.get(quote_id.strip(), ())
            )

    def live_quotes(self, *, client_id: Optional[str] = None) -> Tuple[DealerQuote, ...]:
        with self._lock:
            values = (
                item
                for item in self._quotes.values()
                if item.state == QuoteState.LIVE
                and (client_id is None or item.client_id == client_id)
            )
            return tuple(sorted(values, key=lambda item: item.expires_at))

    @staticmethod
    def _spread(
        policy: QuotePolicy,
        client_id: str,
        quantity: Decimal,
        context: PricingContext,
    ) -> Decimal:
        spread = policy.base_spread_pips
        for threshold, markup in policy.size_breaks:
            if quantity >= threshold:
                spread += markup
        client_markup = dict(policy.client_markups).get(client_id, Decimal("0"))
        spread += client_markup
        spread += Decimal(str(context.volatility_pips)) * policy.volatility_multiplier
        spread += Decimal(str(context.event_risk_pips))
        spread /= Decimal(str(context.liquidity_score))
        return min(policy.maximum_spread_pips, max(policy.minimum_spread_pips, spread))

    def _policy_for(self, symbol: str) -> QuotePolicy:
        matches = [
            item
            for item in self._policies.values()
            if item.symbol == symbol and item.enabled
        ]
        if len(matches) != 1:
            raise ValidationError("symbol does not have exactly one enabled quote policy")
        return matches[0]

    def _require_policy(self, policy_id: str) -> QuotePolicy:
        value = self._policies.get(text(policy_id, "policy id"))
        if value is None:
            raise NotFoundError("quote policy does not exist")
        return value

    def _require_quote(self, quote_id: str) -> DealerQuote:
        value = self._quotes.get(text(quote_id, "quote id"))
        if value is None:
            raise NotFoundError("quote does not exist")
        return value

    @staticmethod
    def _quote_identity(value: DealerQuote) -> tuple[object, ...]:
        return (
            value.request_id,
            value.client_id,
            value.symbol,
            value.side,
            value.quantity,
            value.strategy,
        )
