"""Inventory hedge planning and execution lifecycle."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from threading import RLock
from typing import Dict, Iterable, Mapping, Optional, Tuple

from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError
from forex_monitor.marketmaking.inventory import InventoryLimit
from forex_monitor.marketmaking.models import HedgeOrder, HedgeState, InventoryPosition, number, text, timestamp


@dataclass(frozen=True)
class HedgePolicy:
    policy_id: str
    reporting_currency: str
    trigger_ratio: Decimal = Decimal("0.75")
    target_ratio: Decimal = Decimal("0.25")
    maximum_order_size: Decimal = Decimal("1000000")
    minimum_order_size: Decimal = Decimal("1000")
    passive_inside_soft_limit: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "policy_id", text(self.policy_id, "hedge policy id"))
        reporting = text(self.reporting_currency, "reporting currency").upper()
        if len(reporting) != 3 or not reporting.isalpha():
            raise ValidationError("reporting currency is invalid")
        trigger = number(self.trigger_ratio, "trigger ratio", positive=True)
        target = number(self.target_ratio, "target ratio", non_negative=True)
        if trigger > 1 or target >= trigger:
            raise ValidationError("hedge target ratio must be below trigger ratio")
        minimum = number(self.minimum_order_size, "minimum hedge size", positive=True)
        maximum = number(self.maximum_order_size, "maximum hedge size", positive=True)
        if minimum > maximum:
            raise ValidationError("minimum hedge size exceeds maximum")
        object.__setattr__(self, "reporting_currency", reporting)
        object.__setattr__(self, "trigger_ratio", trigger)
        object.__setattr__(self, "target_ratio", target)
        object.__setattr__(self, "minimum_order_size", minimum)
        object.__setattr__(self, "maximum_order_size", maximum)


@dataclass(frozen=True)
class HedgeExecution:
    execution_id: str
    hedge_id: str
    quantity: Decimal
    price: Decimal
    venue: str
    executed_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "execution_id", text(self.execution_id, "execution id"))
        object.__setattr__(self, "hedge_id", text(self.hedge_id, "hedge id"))
        object.__setattr__(self, "quantity", number(self.quantity, "execution quantity", positive=True))
        object.__setattr__(self, "price", number(self.price, "execution price", positive=True))
        object.__setattr__(self, "venue", text(self.venue, "execution venue").upper())
        object.__setattr__(self, "executed_at", timestamp(self.executed_at, "executed at"))


class HedgePlanner:
    def __init__(self, policy: HedgePolicy) -> None:
        if not isinstance(policy, HedgePolicy):
            raise ValidationError("policy must be HedgePolicy")
        self.policy = policy
        self._orders: Dict[str, HedgeOrder] = {}
        self._plan_index: Dict[str, Tuple[str, ...]] = {}
        self._executions: Dict[str, HedgeExecution] = {}
        self._order_executions: Dict[str, list[str]] = {}
        self._lock = RLock()

    def plan(
        self,
        *,
        plan_id: str,
        positions: Iterable[InventoryPosition],
        limits: Mapping[str, InventoryLimit],
        created_at: datetime,
        limit_prices: Optional[Mapping[str, object]] = None,
    ) -> Tuple[HedgeOrder, ...]:
        key = text(plan_id, "hedge plan id")
        instant = timestamp(created_at, "created at")
        position_values = tuple(sorted(positions, key=lambda item: item.currency))
        with self._lock:
            existing_ids = self._plan_index.get(key)
            if existing_ids is not None:
                return tuple(self._orders[item] for item in existing_ids)
            orders = []
            counter = 0
            for position in position_values:
                if position.currency == self.policy.reporting_currency:
                    continue
                limit = limits.get(position.currency)
                if limit is None or not limit.enabled:
                    continue
                deviation = position.quantity - limit.target
                if abs(deviation) < limit.hard_limit * self.policy.trigger_ratio:
                    continue
                residual = limit.hard_limit * self.policy.target_ratio
                desired = max(Decimal("0"), abs(deviation) - residual)
                direction = "sell" if deviation > 0 else "buy"
                symbol = position.currency + self.policy.reporting_currency
                price_value = None
                if limit_prices and symbol in limit_prices:
                    price_value = number(limit_prices[symbol], "hedge limit price", positive=True)
                while desired >= self.policy.minimum_order_size:
                    counter += 1
                    size = min(desired, self.policy.maximum_order_size)
                    orders.append(
                        HedgeOrder(
                            hedge_id="%s:%03d" % (key, counter),
                            currency=position.currency,
                            side=direction,
                            quantity=size,
                            target_currency=self.policy.reporting_currency,
                            symbol=symbol,
                            limit_price=price_value,
                            venue=None,
                            reason="inventory %.8s exceeds hedge trigger" % position.currency,
                            created_at=instant,
                        )
                    )
                    desired -= size
            ids = tuple(item.hedge_id for item in orders)
            self._plan_index[key] = ids
            for order in orders:
                self._orders[order.hedge_id] = order
                self._order_executions[order.hedge_id] = []
            return tuple(orders)

    def route(self, hedge_id: str, venue: str, *, expected_version: int) -> HedgeOrder:
        with self._lock:
            current = self._require(hedge_id)
            self._check_version(current, expected_version)
            if current.state != HedgeState.PLANNED:
                raise ValidationError("only planned hedges can be routed")
            changed = replace(current, venue=text(venue, "venue").upper(), state=HedgeState.ROUTED, version=current.version + 1)
            self._orders[current.hedge_id] = changed
            return changed

    def record_execution(self, execution: HedgeExecution) -> HedgeOrder:
        if not isinstance(execution, HedgeExecution):
            raise ValidationError("execution must be HedgeExecution")
        with self._lock:
            current = self._require(execution.hedge_id)
            existing = self._executions.get(execution.execution_id)
            if existing is not None:
                if existing == execution:
                    return current
                raise StorageConflictError("hedge execution id already exists")
            if current.state not in {HedgeState.ROUTED, HedgeState.PARTIALLY_FILLED}:
                raise ValidationError("hedge is not executable")
            if current.venue != execution.venue:
                raise ValidationError("execution venue does not match routed venue")
            total = current.filled_quantity + execution.quantity
            if total > current.quantity:
                raise ValidationError("hedge execution exceeds remaining quantity")
            previous_notional = (current.average_fill_price or Decimal("0")) * current.filled_quantity
            average = (previous_notional + execution.price * execution.quantity) / total
            state = HedgeState.FILLED if total == current.quantity else HedgeState.PARTIALLY_FILLED
            changed = replace(current, filled_quantity=total, average_fill_price=average, state=state, version=current.version + 1)
            self._orders[current.hedge_id] = changed
            self._executions[execution.execution_id] = execution
            self._order_executions[current.hedge_id].append(execution.execution_id)
            return changed

    def cancel(self, hedge_id: str, *, expected_version: int) -> HedgeOrder:
        with self._lock:
            current = self._require(hedge_id)
            self._check_version(current, expected_version)
            if current.state not in {HedgeState.PLANNED, HedgeState.ROUTED, HedgeState.PARTIALLY_FILLED}:
                raise ValidationError("hedge cannot be cancelled from its current state")
            changed = replace(current, state=HedgeState.CANCELLED, version=current.version + 1)
            self._orders[current.hedge_id] = changed
            return changed

    def fail(self, hedge_id: str, *, expected_version: int, reason: str) -> HedgeOrder:
        text(reason, "failure reason")
        with self._lock:
            current = self._require(hedge_id)
            self._check_version(current, expected_version)
            if current.state in {HedgeState.FILLED, HedgeState.CANCELLED}:
                raise ValidationError("completed hedge cannot fail")
            changed = replace(current, state=HedgeState.FAILED, reason="%s; failed: %s" % (current.reason, reason), version=current.version + 1)
            self._orders[current.hedge_id] = changed
            return changed

    def order(self, hedge_id: str) -> Optional[HedgeOrder]:
        with self._lock:
            return self._orders.get(hedge_id.strip())

    def orders(self, *, state: Optional[HedgeState] = None) -> Tuple[HedgeOrder, ...]:
        with self._lock:
            values = [item for item in self._orders.values() if state is None or item.state == state]
            return tuple(sorted(values, key=lambda item: (item.created_at, item.hedge_id)))

    def executions(self, hedge_id: str) -> Tuple[HedgeExecution, ...]:
        with self._lock:
            return tuple(self._executions[item] for item in self._order_executions.get(hedge_id.strip(), ()))

    def _require(self, hedge_id: str) -> HedgeOrder:
        value = self._orders.get(text(hedge_id, "hedge id"))
        if value is None:
            raise NotFoundError("hedge order does not exist")
        return value

    @staticmethod
    def _check_version(order: HedgeOrder, expected_version: int) -> None:
        if order.version != expected_version:
            raise StorageConflictError("hedge order version does not match")
