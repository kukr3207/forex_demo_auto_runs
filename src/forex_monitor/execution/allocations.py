"""Post-trade fill allocation with exact remainder handling."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Dict, Iterable, Mapping, Optional, Sequence, Tuple

from forex_monitor.errors import StorageConflictError, ValidationError
from forex_monitor.execution.fills import Fill
from forex_monitor.models import as_decimal


@dataclass(frozen=True)
class AllocationRequest:
    account_id: str
    weight: Decimal
    minimum_quantity: Decimal = Decimal("0")
    maximum_quantity: Optional[Decimal] = None

    def __post_init__(self) -> None:
        account = self.account_id.strip()
        if not account:
            raise ValidationError("allocation account id cannot be blank")
        weight = as_decimal(self.weight, "allocation weight", positive=True)
        minimum = as_decimal(self.minimum_quantity, "minimum allocation")
        maximum = as_decimal(self.maximum_quantity, "maximum allocation", positive=True) if self.maximum_quantity is not None else None
        if minimum < 0:
            raise ValidationError("minimum allocation cannot be negative")
        if maximum is not None and maximum < minimum:
            raise ValidationError("maximum allocation cannot be below minimum")
        object.__setattr__(self, "account_id", account)
        object.__setattr__(self, "weight", weight)
        object.__setattr__(self, "minimum_quantity", minimum)
        object.__setattr__(self, "maximum_quantity", maximum)


@dataclass(frozen=True)
class Allocation:
    allocation_id: str
    fill_id: str
    account_id: str
    quantity: Decimal
    price: Decimal
    commission: Decimal

    @property
    def notional(self) -> Decimal:
        return self.quantity * self.price


def allocate_fill(
    fill: Fill,
    requests: Sequence[AllocationRequest],
    *,
    quantity_increment: object = Decimal("0.01"),
) -> Tuple[Allocation, ...]:
    if not requests:
        raise ValidationError("fill allocation requires at least one account")
    if len({request.account_id for request in requests}) != len(requests):
        raise ValidationError("allocation account ids must be unique")
    increment = as_decimal(quantity_increment, "quantity increment", positive=True)
    total_weight = sum((request.weight for request in requests), Decimal("0"))
    quantities: Dict[str, Decimal] = {}
    fractions = []
    allocated = Decimal("0")
    for request in requests:
        exact = fill.quantity * request.weight / total_weight
        rounded = (exact / increment).to_integral_value(rounding=ROUND_DOWN) * increment
        rounded = max(rounded, request.minimum_quantity)
        if request.maximum_quantity is not None:
            rounded = min(rounded, request.maximum_quantity)
        quantities[request.account_id] = rounded
        fractions.append((exact - rounded, request.account_id))
        allocated += rounded
    if allocated > fill.quantity:
        raise ValidationError("allocation minimums exceed the fill quantity")
    remainder = fill.quantity - allocated
    for _, account_id in sorted(fractions, reverse=True):
        if remainder <= 0:
            break
        request = next(item for item in requests if item.account_id == account_id)
        available = remainder
        if request.maximum_quantity is not None:
            available = min(available, request.maximum_quantity - quantities[account_id])
        addition = (available / increment).to_integral_value(rounding=ROUND_DOWN) * increment
        quantities[account_id] += addition
        remainder -= addition
    if remainder:
        for request in requests:
            if request.maximum_quantity is None or quantities[request.account_id] + remainder <= request.maximum_quantity:
                quantities[request.account_id] += remainder
                remainder = Decimal("0")
                break
    if remainder:
        raise ValidationError("allocation maximums cannot absorb the fill quantity")
    commission_allocated = Decimal("0")
    result = []
    for index, request in enumerate(requests):
        quantity = quantities[request.account_id]
        if index == len(requests) - 1:
            commission = fill.commission - commission_allocated
        else:
            commission = fill.commission * quantity / fill.quantity
            commission_allocated += commission
        result.append(
            Allocation(
                "%s:%s" % (fill.fill_id, request.account_id),
                fill.fill_id,
                request.account_id,
                quantity,
                fill.price,
                commission,
            )
        )
    return tuple(result)


class AllocationBook:
    def __init__(self) -> None:
        self._allocations: Dict[str, Allocation] = {}
        self._fills: Dict[str, Tuple[str, ...]] = {}

    def book(self, allocations: Sequence[Allocation]) -> Tuple[Allocation, ...]:
        if not allocations:
            raise ValidationError("allocation booking cannot be empty")
        fill_ids = {item.fill_id for item in allocations}
        if len(fill_ids) != 1:
            raise ValidationError("one booking may only contain one fill")
        fill_id = allocations[0].fill_id
        existing_ids = self._fills.get(fill_id)
        if existing_ids:
            existing = tuple(self._allocations[item] for item in existing_ids)
            if existing == tuple(allocations):
                return existing
            raise StorageConflictError("fill already has different allocations")
        if len({item.account_id for item in allocations}) != len(allocations):
            raise ValidationError("an account may only appear once per fill")
        for allocation in allocations:
            if allocation.allocation_id in self._allocations:
                raise StorageConflictError("allocation id already exists")
        for allocation in allocations:
            self._allocations[allocation.allocation_id] = allocation
        self._fills[fill_id] = tuple(item.allocation_id for item in allocations)
        return tuple(allocations)

    def for_fill(self, fill_id: str) -> Tuple[Allocation, ...]:
        return tuple(self._allocations[item] for item in self._fills.get(fill_id, ()))

    def for_account(self, account_id: str) -> Tuple[Allocation, ...]:
        return tuple(sorted(
            (item for item in self._allocations.values() if item.account_id == account_id),
            key=lambda item: item.allocation_id,
        ))

    def notionals_by_account(self) -> Mapping[str, Decimal]:
        result: Dict[str, Decimal] = {}
        for allocation in self._allocations.values():
            result[allocation.account_id] = result.get(allocation.account_id, Decimal("0")) + allocation.notional
        return result
