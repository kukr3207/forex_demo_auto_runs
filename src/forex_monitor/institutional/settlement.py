"""Payment-versus-payment instruction generation and settlement."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from threading import RLock
from typing import Dict, Iterable, Mapping, Optional, Tuple

from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError
from forex_monitor.institutional.lifecycle import TradeLifecycleService
from forex_monitor.institutional.models import (
    InstitutionalTrade,
    SettlementInstruction,
    SettlementStatus,
    TradeState,
    aware,
    required,
)


@dataclass(frozen=True)
class SettlementAccount:
    account_id: str
    client_id: str
    currency: str
    bank_identifier: str
    account_number: str
    correspondent: Optional[str] = None
    active: bool = True
    cutoff_hour_utc: int = 16

    def __post_init__(self) -> None:
        if not self.account_id.strip() or not self.client_id.strip():
            raise ValidationError("settlement account identity is required")
        if len(self.currency.strip()) != 3:
            raise ValidationError("settlement account currency is invalid")
        if not self.bank_identifier.strip() or not self.account_number.strip():
            raise ValidationError("bank and account identifiers are required")
        if self.cutoff_hour_utc < 0 or self.cutoff_hour_utc > 23:
            raise ValidationError("settlement cutoff hour is invalid")


@dataclass(frozen=True)
class SettlementPair:
    trade_id: str
    pay_instruction_id: str
    receive_instruction_id: str


class SettlementService:
    def __init__(self, lifecycle: TradeLifecycleService) -> None:
        self._lifecycle = lifecycle
        self._accounts: Dict[str, SettlementAccount] = {}
        self._instructions: Dict[str, SettlementInstruction] = {}
        self._trade_instructions: Dict[str, list[str]] = {}
        self._external_references: Dict[str, str] = {}
        self._lock = RLock()

    def register_account(self, account: SettlementAccount) -> SettlementAccount:
        if not isinstance(account, SettlementAccount):
            raise ValidationError("account must be SettlementAccount")
        with self._lock:
            existing = self._accounts.get(account.account_id)
            if existing is not None:
                if existing == account:
                    return existing
                raise StorageConflictError("settlement account already exists")
            self._accounts[account.account_id] = account
            return account

    def create_instructions(
        self,
        trade_id: str,
        *,
        pay_instruction_id: str,
        receive_instruction_id: str,
        account_ids: Mapping[str, str],
    ) -> SettlementPair:
        trade = self._require_trade(trade_id)
        if trade.state != TradeState.MATCHED:
            raise ValidationError("trade must be matched before settlement instruction")
        pay_currency, pay_amount, receive_currency, receive_amount = self._cashflows(trade)
        pay_account = self._account_for(trade.client_id, pay_currency, account_ids)
        receive_account = self._account_for(trade.client_id, receive_currency, account_ids)
        pay = SettlementInstruction(
            instruction_id=pay_instruction_id,
            trade_id=trade.trade_id,
            currency=pay_currency,
            amount=pay_amount,
            direction="pay",
            value_date=trade.settlement_date,
            account_id=pay_account.account_id,
            correspondent=pay_account.correspondent,
        )
        receive = SettlementInstruction(
            instruction_id=receive_instruction_id,
            trade_id=trade.trade_id,
            currency=receive_currency,
            amount=receive_amount,
            direction="receive",
            value_date=trade.settlement_date,
            account_id=receive_account.account_id,
            correspondent=receive_account.correspondent,
        )
        with self._lock:
            current = self._trade_instructions.get(trade.trade_id)
            if current:
                existing = tuple(self._instructions[item] for item in current)
                if existing == (pay, receive):
                    return SettlementPair(trade.trade_id, pay_instruction_id, receive_instruction_id)
                raise StorageConflictError("trade already has settlement instructions")
            if pay_instruction_id in self._instructions or receive_instruction_id in self._instructions:
                raise StorageConflictError("settlement instruction id already exists")
            self._instructions[pay.instruction_id] = pay
            self._instructions[receive.instruction_id] = receive
            self._trade_instructions[trade.trade_id] = [pay.instruction_id, receive.instruction_id]
            return SettlementPair(trade.trade_id, pay.instruction_id, receive.instruction_id)

    def release(
        self,
        trade_id: str,
        *,
        released_at: datetime,
        lifecycle_event_id: str,
        actor_id: str,
        expected_trade_version: int,
    ) -> Tuple[SettlementInstruction, ...]:
        timestamp = aware(released_at, "released at")
        with self._lock:
            instructions = self._require_trade_instructions(trade_id)
            if any(item.status != SettlementStatus.DRAFT for item in instructions):
                raise ValidationError("all instructions must be draft before release")
            for item in instructions:
                account = self._accounts[item.account_id]
                if not account.active:
                    raise ValidationError("inactive settlement account cannot be released")
                if timestamp.date() == item.value_date and timestamp.hour >= account.cutoff_hour_utc:
                    raise ValidationError("settlement instruction missed currency cutoff")
            changed = tuple(
                replace(
                    item,
                    status=SettlementStatus.RELEASED,
                    released_at=timestamp,
                    version=item.version + 1,
                )
                for item in instructions
            )
            self._lifecycle.transition(
                trade_id,
                TradeState.SETTLEMENT_PENDING,
                event_id=lifecycle_event_id,
                actor_id=actor_id,
                reason="settlement instructions released",
                occurred_at=timestamp,
                expected_version=expected_trade_version,
            )
            for item in changed:
                self._instructions[item.instruction_id] = item
            return changed

    def acknowledge(
        self,
        instruction_id: str,
        external_reference: str,
        *,
        expected_version: int,
    ) -> SettlementInstruction:
        reference = required(external_reference, "external reference")
        with self._lock:
            current = self._require_instruction(instruction_id)
            if current.version != expected_version:
                raise StorageConflictError("settlement instruction version does not match")
            owner = self._external_references.get(reference)
            if owner and owner != current.instruction_id:
                raise StorageConflictError("external settlement reference already exists")
            if current.status == SettlementStatus.ACKNOWLEDGED:
                if current.external_reference == reference:
                    return current
                raise StorageConflictError("instruction has another external reference")
            if current.status != SettlementStatus.RELEASED:
                raise ValidationError("only released instructions can be acknowledged")
            changed = replace(
                current,
                status=SettlementStatus.ACKNOWLEDGED,
                external_reference=reference,
                version=current.version + 1,
            )
            self._instructions[current.instruction_id] = changed
            self._external_references[reference] = current.instruction_id
            return changed

    def settle(
        self,
        instruction_id: str,
        *,
        settled_at: datetime,
        expected_version: int,
    ) -> SettlementInstruction:
        timestamp = aware(settled_at, "settled at")
        with self._lock:
            current = self._require_instruction(instruction_id)
            if current.version != expected_version:
                raise StorageConflictError("settlement instruction version does not match")
            if current.status == SettlementStatus.SETTLED:
                return current
            if current.status != SettlementStatus.ACKNOWLEDGED:
                raise ValidationError("only acknowledged instructions can settle")
            if current.released_at and timestamp < current.released_at:
                raise ValidationError("settlement cannot predate release")
            changed = replace(
                current,
                status=SettlementStatus.SETTLED,
                settled_at=timestamp,
                version=current.version + 1,
            )
            self._instructions[current.instruction_id] = changed
            return changed

    def complete_trade(
        self,
        trade_id: str,
        *,
        lifecycle_event_id: str,
        actor_id: str,
        occurred_at: datetime,
        expected_trade_version: int,
    ) -> InstitutionalTrade:
        instructions = self.instructions_for(trade_id)
        if len(instructions) != 2 or any(
            item.status != SettlementStatus.SETTLED for item in instructions
        ):
            raise ValidationError("both trade legs must settle before completion")
        return self._lifecycle.transition(
            trade_id,
            TradeState.SETTLED,
            event_id=lifecycle_event_id,
            actor_id=actor_id,
            reason="both settlement legs completed",
            occurred_at=occurred_at,
            expected_version=expected_trade_version,
        )

    def fail(
        self,
        instruction_id: str,
        reason: str,
        *,
        expected_version: int,
    ) -> SettlementInstruction:
        with self._lock:
            current = self._require_instruction(instruction_id)
            if current.version != expected_version:
                raise StorageConflictError("settlement instruction version does not match")
            if current.status in {SettlementStatus.SETTLED, SettlementStatus.CANCELLED}:
                raise ValidationError("completed settlement instruction cannot fail")
            changed = replace(
                current,
                status=SettlementStatus.FAILED,
                failure_reason=required(reason, "failure reason"),
                version=current.version + 1,
            )
            self._instructions[current.instruction_id] = changed
            return changed

    def retry(
        self,
        instruction_id: str,
        *,
        expected_version: int,
    ) -> SettlementInstruction:
        with self._lock:
            current = self._require_instruction(instruction_id)
            if current.version != expected_version:
                raise StorageConflictError("settlement instruction version does not match")
            if current.status != SettlementStatus.FAILED:
                raise ValidationError("only failed settlement instructions can retry")
            changed = replace(
                current,
                status=SettlementStatus.DRAFT,
                failure_reason=None,
                external_reference=None,
                released_at=None,
                version=current.version + 1,
            )
            self._instructions[current.instruction_id] = changed
            return changed

    def instructions_for(self, trade_id: str) -> Tuple[SettlementInstruction, ...]:
        with self._lock:
            return tuple(
                self._instructions[item]
                for item in self._trade_instructions.get(trade_id.strip(), ())
            )

    def due(self, value_date: date) -> Tuple[SettlementInstruction, ...]:
        with self._lock:
            values = (
                item
                for item in self._instructions.values()
                if item.value_date <= value_date
                and item.status not in {
                    SettlementStatus.SETTLED,
                    SettlementStatus.CANCELLED,
                }
            )
            return tuple(sorted(values, key=lambda item: (item.value_date, item.instruction_id)))

    def cash_ladder(self, value_date: date) -> Mapping[str, Decimal]:
        result: Dict[str, Decimal] = {}
        with self._lock:
            for item in self._instructions.values():
                if item.value_date != value_date or item.status == SettlementStatus.CANCELLED:
                    continue
                sign = Decimal("-1") if item.direction == "pay" else Decimal("1")
                result[item.currency] = result.get(item.currency, Decimal("0")) + sign * item.amount
        return result

    @staticmethod
    def _cashflows(trade: InstitutionalTrade) -> Tuple[str, Decimal, str, Decimal]:
        if trade.side == "buy":
            return (
                trade.quote_currency,
                trade.quote_amount,
                trade.base_currency,
                trade.base_amount,
            )
        return (
            trade.base_currency,
            trade.base_amount,
            trade.quote_currency,
            trade.quote_amount,
        )

    def _account_for(
        self,
        client_id: str,
        currency: str,
        account_ids: Mapping[str, str],
    ) -> SettlementAccount:
        account_id = account_ids.get(currency)
        if not account_id:
            raise ValidationError("missing settlement account for %s" % currency)
        account = self._accounts.get(account_id)
        if account is None:
            raise ValidationError("settlement account does not exist")
        if account.client_id != client_id or account.currency != currency:
            raise ValidationError("settlement account does not match client and currency")
        return account

    def _require_trade(self, trade_id: str) -> InstitutionalTrade:
        value = self._lifecycle.trade(trade_id)
        if value is None:
            raise NotFoundError("trade does not exist")
        return value

    def _require_instruction(self, instruction_id: str) -> SettlementInstruction:
        value = self._instructions.get(required(instruction_id, "instruction id"))
        if value is None:
            raise NotFoundError("settlement instruction does not exist")
        return value

    def _require_trade_instructions(
        self,
        trade_id: str,
    ) -> Tuple[SettlementInstruction, ...]:
        values = self.instructions_for(trade_id)
        if len(values) != 2:
            raise NotFoundError("trade settlement instructions do not exist")
        return values
