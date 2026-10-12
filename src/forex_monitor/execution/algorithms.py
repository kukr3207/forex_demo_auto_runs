"""Deterministic TWAP, VWAP, and participation schedule construction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Iterable, List, Mapping, Optional, Sequence, Tuple

from forex_monitor.errors import ValidationError
from forex_monitor.execution.orders import Order, OrderSide, OrderType, TimeInForce
from forex_monitor.models import as_decimal, ensure_utc


class AlgorithmType(str, Enum):
    TWAP = "twap"
    VWAP = "vwap"
    PARTICIPATION = "participation"
    ICEBERG = "iceberg"


@dataclass(frozen=True)
class SliceInstruction:
    slice_number: int
    release_at: datetime
    quantity: Decimal
    limit_price: Optional[Decimal] = None
    participation_rate: Optional[Decimal] = None

    def __post_init__(self) -> None:
        if self.slice_number <= 0:
            raise ValidationError("slice number must be positive")
        object.__setattr__(self, "release_at", ensure_utc(self.release_at, "slice release time"))
        object.__setattr__(self, "quantity", as_decimal(self.quantity, "slice quantity", positive=True))
        if self.limit_price is not None:
            object.__setattr__(self, "limit_price", as_decimal(self.limit_price, "slice limit price", positive=True))
        if self.participation_rate is not None:
            rate = as_decimal(self.participation_rate, "participation rate", positive=True)
            if rate > 1:
                raise ValidationError("participation rate cannot exceed one")
            object.__setattr__(self, "participation_rate", rate)


@dataclass(frozen=True)
class ExecutionPlan:
    order_id: str
    algorithm: AlgorithmType
    starts_at: datetime
    ends_at: datetime
    slices: Tuple[SliceInstruction, ...]

    def __post_init__(self) -> None:
        start = ensure_utc(self.starts_at, "plan start")
        end = ensure_utc(self.ends_at, "plan end")
        if end <= start:
            raise ValidationError("execution plan end must follow start")
        if not self.slices:
            raise ValidationError("execution plan requires at least one slice")
        if tuple(sorted(self.slices, key=lambda item: item.release_at)) != self.slices:
            raise ValidationError("execution slices must be time ordered")
        if self.slices[0].release_at < start or self.slices[-1].release_at > end:
            raise ValidationError("execution slices must fall within the plan window")
        object.__setattr__(self, "starts_at", start)
        object.__setattr__(self, "ends_at", end)

    @property
    def quantity(self) -> Decimal:
        return sum((item.quantity for item in self.slices), Decimal("0"))

    def due(self, now: datetime, released: int = 0) -> Tuple[SliceInstruction, ...]:
        timestamp = ensure_utc(now, "plan check time")
        return tuple(item for item in self.slices[released:] if item.release_at <= timestamp)

    def child_order(self, parent: Order, instruction: SliceInstruction) -> Order:
        if instruction.quantity > parent.remaining_quantity:
            raise ValidationError("slice exceeds the parent order remainder")
        return Order(
            order_id="%s:%04d" % (parent.order_id, instruction.slice_number),
            symbol=parent.symbol,
            side=parent.side,
            quantity=instruction.quantity,
            order_type=OrderType.LIMIT if instruction.limit_price is not None else OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            limit_price=instruction.limit_price,
            account_id=parent.account_id,
            strategy_id=parent.strategy_id,
            parent_order_id=parent.order_id,
            created_at=instruction.release_at,
            updated_at=instruction.release_at,
        )


class ParticipationScheduler:
    @staticmethod
    def twap(order: Order, start: datetime, end: datetime, slice_count: int) -> ExecutionPlan:
        start_time = ensure_utc(start, "TWAP start")
        end_time = ensure_utc(end, "TWAP end")
        if slice_count <= 0:
            raise ValidationError("TWAP slice count must be positive")
        if end_time <= start_time:
            raise ValidationError("TWAP end must follow start")
        interval = (end_time - start_time) / slice_count
        base = order.remaining_quantity / Decimal(slice_count)
        slices = []
        allocated = Decimal("0")
        for index in range(slice_count):
            quantity = order.remaining_quantity - allocated if index == slice_count - 1 else base
            slices.append(SliceInstruction(index + 1, start_time + interval * index, quantity, order.limit_price))
            allocated += quantity
        return ExecutionPlan(order.order_id, AlgorithmType.TWAP, start_time, end_time, tuple(slices))

    @staticmethod
    def vwap(
        order: Order,
        start: datetime,
        interval: timedelta,
        volume_weights: Sequence[object],
    ) -> ExecutionPlan:
        start_time = ensure_utc(start, "VWAP start")
        if interval.total_seconds() <= 0:
            raise ValidationError("VWAP interval must be positive")
        weights = tuple(as_decimal(item, "volume weight") for item in volume_weights)
        if not weights or any(item < 0 for item in weights) or sum(weights) <= 0:
            raise ValidationError("VWAP weights must include positive volume")
        total = sum(weights)
        slices = []
        allocated = Decimal("0")
        for index, weight in enumerate(weights):
            quantity = order.remaining_quantity - allocated if index == len(weights) - 1 else order.remaining_quantity * weight / total
            slices.append(SliceInstruction(index + 1, start_time + interval * index, quantity, order.limit_price))
            allocated += quantity
        end = start_time + interval * len(weights)
        return ExecutionPlan(order.order_id, AlgorithmType.VWAP, start_time, end, tuple(slices))

    @staticmethod
    def participation(
        order: Order,
        observations: Sequence[Tuple[datetime, object]],
        rate: object,
    ) -> ExecutionPlan:
        parsed_rate = as_decimal(rate, "participation rate", positive=True)
        if parsed_rate > 1:
            raise ValidationError("participation rate cannot exceed one")
        if not observations:
            raise ValidationError("participation schedule requires volume observations")
        ordered = tuple(sorted(observations, key=lambda item: item[0]))
        remaining = order.remaining_quantity
        slices = []
        for index, (timestamp, raw_volume) in enumerate(ordered, 1):
            volume = as_decimal(raw_volume, "market volume")
            if volume < 0:
                raise ValidationError("market volume cannot be negative")
            quantity = min(remaining, volume * parsed_rate)
            if quantity > 0:
                slices.append(SliceInstruction(index, ensure_utc(timestamp), quantity, order.limit_price, parsed_rate))
                remaining -= quantity
            if remaining == 0:
                break
        if not slices:
            raise ValidationError("participation observations produced no executable quantity")
        return ExecutionPlan(
            order.order_id,
            AlgorithmType.PARTICIPATION,
            slices[0].release_at,
            slices[-1].release_at + timedelta(microseconds=1),
            tuple(slices),
        )
