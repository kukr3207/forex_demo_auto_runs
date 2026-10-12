"""Tiered pricing schedules for executions, custody, and subscriptions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from threading import RLock
from typing import Dict, Iterable, Mapping, Optional, Tuple

from forex_monitor.accounting.models import FeeCharge, currency, money, text, utc
from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError


@dataclass(frozen=True)
class FeeTier:
    minimum_quantity: Decimal
    rate: Decimal
    flat_amount: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        object.__setattr__(self, "minimum_quantity", money(self.minimum_quantity, "tier minimum", non_negative=True))
        object.__setattr__(self, "rate", money(self.rate, "tier rate", non_negative=True))
        object.__setattr__(self, "flat_amount", money(self.flat_amount, "flat amount", non_negative=True))


@dataclass(frozen=True)
class FeeSchedule:
    schedule_id: str
    client_id: str
    charge_type: str
    currency: str
    calculation: str
    tiers: Tuple[FeeTier, ...]
    minimum_charge: Decimal = Decimal("0")
    maximum_charge: Optional[Decimal] = None
    effective_from: Optional[datetime] = None
    effective_until: Optional[datetime] = None
    tax_rate: Decimal = Decimal("0")
    active: bool = True
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "schedule_id", text(self.schedule_id, "schedule id"))
        object.__setattr__(self, "client_id", text(self.client_id, "client id"))
        object.__setattr__(self, "charge_type", text(self.charge_type, "charge type").lower())
        object.__setattr__(self, "currency", currency(self.currency))
        calculation = text(self.calculation, "calculation").lower()
        if calculation not in {"notional", "quantity", "flat"}:
            raise ValidationError("fee calculation is invalid")
        object.__setattr__(self, "calculation", calculation)
        tiers = tuple(sorted(self.tiers, key=lambda item: item.minimum_quantity))
        if not tiers or tiers[0].minimum_quantity != 0:
            raise ValidationError("fee tiers must start at zero")
        if len({item.minimum_quantity for item in tiers}) != len(tiers):
            raise ValidationError("fee schedule has duplicate tier boundaries")
        object.__setattr__(self, "tiers", tiers)
        minimum = money(self.minimum_charge, "minimum charge", non_negative=True)
        maximum = money(self.maximum_charge, "maximum charge", positive=True) if self.maximum_charge is not None else None
        if maximum is not None and minimum > maximum:
            raise ValidationError("minimum charge exceeds maximum")
        object.__setattr__(self, "minimum_charge", minimum)
        object.__setattr__(self, "maximum_charge", maximum)
        if self.effective_from:
            object.__setattr__(self, "effective_from", utc(self.effective_from, "effective from"))
        if self.effective_until:
            until = utc(self.effective_until, "effective until")
            if self.effective_from and until <= self.effective_from:
                raise ValidationError("fee schedule end must follow start")
            object.__setattr__(self, "effective_until", until)
        tax = money(self.tax_rate, "tax rate", non_negative=True)
        if tax > 1:
            raise ValidationError("tax rate cannot exceed one")
        object.__setattr__(self, "tax_rate", tax)


@dataclass(frozen=True)
class FeeCalculation:
    charge: FeeCharge
    tax_amount: Decimal
    gross_amount: Decimal
    tier_minimum: Decimal


class FeeEngine:
    def __init__(self) -> None:
        self._schedules: Dict[str, FeeSchedule] = {}
        self._charges: Dict[str, FeeCalculation] = {}
        self._source_index: Dict[Tuple[str, str, str], str] = {}
        self._lock = RLock()

    def add_schedule(self, schedule: FeeSchedule) -> FeeSchedule:
        if not isinstance(schedule, FeeSchedule):
            raise ValidationError("schedule must be FeeSchedule")
        with self._lock:
            existing = self._schedules.get(schedule.schedule_id)
            if existing is not None:
                if existing == schedule:
                    return existing
                raise StorageConflictError("fee schedule id already exists")
            for item in self._schedules.values():
                if item.client_id == schedule.client_id and item.charge_type == schedule.charge_type and item.active and schedule.active:
                    if self._overlaps(item, schedule):
                        raise StorageConflictError("active fee schedules overlap")
            self._schedules[schedule.schedule_id] = schedule
            return schedule

    def calculate(
        self,
        *,
        charge_id: str,
        client_id: str,
        charge_type: str,
        quantity: object,
        notional: object,
        occurred_at: datetime,
        source_reference: str,
        description: str = "",
    ) -> FeeCalculation:
        instant = utc(occurred_at, "occurred at")
        size = money(quantity, "quantity", non_negative=True)
        value = money(notional, "notional", non_negative=True)
        with self._lock:
            existing = self._charges.get(charge_id)
            if existing is not None:
                return existing
            source_key = (client_id, charge_type.lower(), source_reference)
            duplicate = self._source_index.get(source_key)
            if duplicate:
                return self._charges[duplicate]
            schedule = self._active_schedule(client_id, charge_type, instant)
            basis = value if schedule.calculation == "notional" else size
            tier = self._tier(schedule, basis)
            if schedule.calculation == "flat":
                raw_amount = tier.flat_amount
            else:
                raw_amount = basis * tier.rate + tier.flat_amount
            amount = max(schedule.minimum_charge, raw_amount)
            if schedule.maximum_charge is not None:
                amount = min(amount, schedule.maximum_charge)
            amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            tax = (amount * schedule.tax_rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            charge = FeeCharge(charge_id, schedule.schedule_id, client_id, charge_type, schedule.currency, basis, tier.rate, amount, instant, source_reference, description)
            calculation = FeeCalculation(charge, tax, amount + tax, tier.minimum_quantity)
            self._charges[charge_id] = calculation
            self._source_index[source_key] = charge_id
            return calculation

    def charge(self, charge_id: str) -> Optional[FeeCalculation]:
        with self._lock:
            return self._charges.get(charge_id.strip())

    def charges(
        self,
        *,
        client_id: Optional[str] = None,
        from_time: Optional[datetime] = None,
        to_time: Optional[datetime] = None,
    ) -> Tuple[FeeCalculation, ...]:
        with self._lock:
            values = [
                item for item in self._charges.values()
                if (client_id is None or item.charge.client_id == client_id)
                and (from_time is None or item.charge.occurred_at >= from_time)
                and (to_time is None or item.charge.occurred_at < to_time)
            ]
            return tuple(sorted(values, key=lambda item: (item.charge.occurred_at, item.charge.charge_id)))

    def _active_schedule(self, client_id: str, charge_type: str, at: datetime) -> FeeSchedule:
        matches = [
            item for item in self._schedules.values()
            if item.client_id == client_id
            and item.charge_type == charge_type.lower()
            and item.active
            and (item.effective_from is None or item.effective_from <= at)
            and (item.effective_until is None or at < item.effective_until)
        ]
        if not matches:
            raise NotFoundError("no active fee schedule matches the charge")
        if len(matches) > 1:
            raise StorageConflictError("multiple fee schedules match the charge")
        return matches[0]

    @staticmethod
    def _tier(schedule: FeeSchedule, basis: Decimal) -> FeeTier:
        result = schedule.tiers[0]
        for tier in schedule.tiers:
            if basis >= tier.minimum_quantity:
                result = tier
            else:
                break
        return result

    @staticmethod
    def _overlaps(left: FeeSchedule, right: FeeSchedule) -> bool:
        left_start = left.effective_from or datetime.min.replace(tzinfo=right.effective_from.tzinfo if right.effective_from else None)
        right_start = right.effective_from or datetime.min.replace(tzinfo=left.effective_from.tzinfo if left.effective_from else None)
        left_end = left.effective_until
        right_end = right.effective_until
        return (left_end is None or right_start < left_end) and (right_end is None or left_start < right_end)
