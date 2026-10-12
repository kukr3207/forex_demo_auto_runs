"""Payment-versus-payment settlement lifecycle and exception tracking."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Dict, Mapping, Optional, Sequence, Tuple

from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError
from forex_monitor.models import as_decimal, ensure_utc
from forex_monitor.treasury.cash import _currency


class SettlementState(str, Enum):
    PENDING = "pending"
    MATCHED = "matched"
    RELEASED = "released"
    PARTIALLY_SETTLED = "partially_settled"
    SETTLED = "settled"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {SettlementState.SETTLED, SettlementState.CANCELLED}


@dataclass(frozen=True)
class SettlementLeg:
    currency: str
    amount: Decimal
    payer_account: str
    receiver_account: str
    settled_amount: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        amount = as_decimal(self.amount, "settlement amount", positive=True)
        settled = as_decimal(self.settled_amount, "settled amount")
        if settled < 0 or settled > amount:
            raise ValidationError("settled amount must be within the leg amount")
        if self.payer_account == self.receiver_account:
            raise ValidationError("settlement payer and receiver accounts must differ")
        object.__setattr__(self, "currency", _currency(self.currency))
        object.__setattr__(self, "amount", amount)
        object.__setattr__(self, "settled_amount", settled)

    @property
    def outstanding(self) -> Decimal:
        return self.amount - self.settled_amount


@dataclass(frozen=True)
class SettlementInstruction:
    instruction_id: str
    trade_id: str
    value_date: date
    counterparty: str
    pay_leg: SettlementLeg
    receive_leg: SettlementLeg
    state: SettlementState = SettlementState.PENDING
    version: int = 1
    last_updated: Optional[datetime] = None
    failure_reason: Optional[str] = None

    def __post_init__(self) -> None:
        if not all((self.instruction_id.strip(), self.trade_id.strip(), self.counterparty.strip())):
            raise ValidationError("settlement identifiers cannot be blank")
        if self.pay_leg.currency == self.receive_leg.currency:
            raise ValidationError("FX settlement legs must use different currencies")
        if self.version <= 0:
            raise ValidationError("settlement version must be positive")
        state = self.state if isinstance(self.state, SettlementState) else SettlementState(self.state)
        updated = ensure_utc(self.last_updated, "settlement update time") if self.last_updated else None
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "last_updated", updated)

    @property
    def complete(self) -> bool:
        return self.pay_leg.outstanding == 0 and self.receive_leg.outstanding == 0


@dataclass(frozen=True)
class SettlementEvent:
    instruction_id: str
    state: SettlementState
    occurred_at: datetime
    message: str
    version: int


TRANSITIONS = {
    SettlementState.PENDING: {SettlementState.MATCHED, SettlementState.CANCELLED, SettlementState.FAILED},
    SettlementState.MATCHED: {SettlementState.RELEASED, SettlementState.CANCELLED, SettlementState.FAILED},
    SettlementState.RELEASED: {SettlementState.PARTIALLY_SETTLED, SettlementState.SETTLED, SettlementState.FAILED},
    SettlementState.PARTIALLY_SETTLED: {SettlementState.PARTIALLY_SETTLED, SettlementState.SETTLED, SettlementState.FAILED},
    SettlementState.FAILED: {SettlementState.MATCHED, SettlementState.CANCELLED},
}


class SettlementBook:
    def __init__(self) -> None:
        self._instructions: Dict[str, SettlementInstruction] = {}
        self._trade_ids: Dict[str, str] = {}
        self._events: Dict[str, list[SettlementEvent]] = {}

    def add(self, instruction: SettlementInstruction) -> SettlementInstruction:
        existing = self._instructions.get(instruction.instruction_id)
        if existing is not None:
            if existing == instruction:
                return existing
            raise StorageConflictError("settlement instruction id already exists")
        existing_instruction = self._trade_ids.get(instruction.trade_id)
        if existing_instruction:
            raise StorageConflictError("trade already has a settlement instruction")
        self._instructions[instruction.instruction_id] = instruction
        self._trade_ids[instruction.trade_id] = instruction.instruction_id
        self._events[instruction.instruction_id] = []
        return instruction

    def get(self, instruction_id: str) -> SettlementInstruction:
        try:
            return self._instructions[instruction_id]
        except KeyError as exc:
            raise NotFoundError("settlement instruction was not found") from exc

    def transition(
        self,
        instruction_id: str,
        state: SettlementState,
        at: datetime,
        message: str,
        *,
        expected_version: Optional[int] = None,
    ) -> SettlementInstruction:
        current = self.get(instruction_id)
        target = state if isinstance(state, SettlementState) else SettlementState(state)
        if expected_version is not None and current.version != expected_version:
            raise StorageConflictError("settlement instruction version changed")
        if target not in TRANSITIONS.get(current.state, set()):
            raise ValidationError("settlement state transition is not allowed")
        timestamp = ensure_utc(at, "settlement event time")
        if current.last_updated and timestamp < current.last_updated:
            raise ValidationError("settlement event time cannot move backward")
        reason = message.strip() if target is SettlementState.FAILED else None
        changed = replace(
            current,
            state=target,
            version=current.version + 1,
            last_updated=timestamp,
            failure_reason=reason,
        )
        self._instructions[instruction_id] = changed
        self._events[instruction_id].append(SettlementEvent(instruction_id, target, timestamp, message.strip(), changed.version))
        return changed

    def settle_leg(
        self,
        instruction_id: str,
        *,
        pay_amount: object = Decimal("0"),
        receive_amount: object = Decimal("0"),
        at: datetime,
    ) -> SettlementInstruction:
        current = self.get(instruction_id)
        if current.state not in {SettlementState.RELEASED, SettlementState.PARTIALLY_SETTLED}:
            raise ValidationError("settlement instruction has not been released")
        pay = as_decimal(pay_amount, "paid amount")
        receive = as_decimal(receive_amount, "received amount")
        if pay < 0 or receive < 0 or (pay == 0 and receive == 0):
            raise ValidationError("settlement update requires a positive leg amount")
        if pay > current.pay_leg.outstanding or receive > current.receive_leg.outstanding:
            raise ValidationError("settlement update exceeds an outstanding leg")
        pay_leg = replace(current.pay_leg, settled_amount=current.pay_leg.settled_amount + pay)
        receive_leg = replace(current.receive_leg, settled_amount=current.receive_leg.settled_amount + receive)
        complete = pay_leg.outstanding == 0 and receive_leg.outstanding == 0
        state = SettlementState.SETTLED if complete else SettlementState.PARTIALLY_SETTLED
        changed = replace(
            current,
            pay_leg=pay_leg,
            receive_leg=receive_leg,
            state=state,
            version=current.version + 1,
            last_updated=ensure_utc(at, "settlement event time"),
        )
        self._instructions[instruction_id] = changed
        self._events[instruction_id].append(SettlementEvent(instruction_id, state, changed.last_updated, "settlement amounts recorded", changed.version))
        return changed

    def events(self, instruction_id: str) -> Tuple[SettlementEvent, ...]:
        self.get(instruction_id)
        return tuple(self._events[instruction_id])

    def due(self, value_date: date, *, counterparty: Optional[str] = None) -> Tuple[SettlementInstruction, ...]:
        return tuple(sorted(
            (
                item for item in self._instructions.values()
                if item.value_date <= value_date and not item.state.terminal
                and (counterparty is None or item.counterparty == counterparty)
            ),
            key=lambda item: (item.value_date, item.counterparty, item.instruction_id),
        ))

    def exposure(self, counterparty: str) -> Mapping[str, Decimal]:
        result: Dict[str, Decimal] = {}
        for item in self._instructions.values():
            if item.counterparty != counterparty or item.state.terminal:
                continue
            result[item.pay_leg.currency] = result.get(item.pay_leg.currency, Decimal("0")) - item.pay_leg.outstanding
            result[item.receive_leg.currency] = result.get(item.receive_leg.currency, Decimal("0")) + item.receive_leg.outstanding
        return result
