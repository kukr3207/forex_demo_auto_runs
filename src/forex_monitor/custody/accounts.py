"""Custody cash ledger with value-date balances and reservations."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from threading import RLock
from typing import Dict, Iterable, Optional, Tuple

from forex_monitor.custody.models import AccountBalance, AccountStatus, CashMovement, CashReservation, CustodyAccount, text, utc
from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError


class CustodyLedger:
    def __init__(self) -> None:
        self._accounts: Dict[str, CustodyAccount] = {}
        self._movements: Dict[str, CashMovement] = {}
        self._account_movements: Dict[str, list[str]] = {}
        self._source_index: Dict[Tuple[str, str], str] = {}
        self._reservations: Dict[str, CashReservation] = {}
        self._reference_reservations: Dict[Tuple[str, str], str] = {}
        self._lock = RLock()

    def open_account(self, account: CustodyAccount, *, opened_at: datetime) -> CustodyAccount:
        instant = utc(opened_at, "opened at")
        with self._lock:
            existing = self._accounts.get(account.account_id)
            if existing is not None:
                return existing
            if account.status != AccountStatus.PENDING:
                raise ValidationError("new custody account must be pending")
            changed = replace(account, status=AccountStatus.ACTIVE, opened_at=instant, version=account.version + 1)
            self._accounts[account.account_id] = changed
            self._account_movements[account.account_id] = []
            return changed

    def restrict(self, account_id: str, *, expected_version: int) -> CustodyAccount:
        with self._lock:
            current = self.require_account(account_id)
            if current.version != expected_version:
                raise StorageConflictError("custody account version does not match")
            if current.status != AccountStatus.ACTIVE:
                raise ValidationError("only active accounts can be restricted")
            changed = replace(current, status=AccountStatus.RESTRICTED, version=current.version + 1)
            self._accounts[current.account_id] = changed
            return changed

    def post(self, movement: CashMovement) -> CashMovement:
        if not isinstance(movement, CashMovement):
            raise ValidationError("movement must be CashMovement")
        with self._lock:
            existing = self._movements.get(movement.movement_id)
            if existing is not None:
                if existing == movement:
                    return existing
                raise StorageConflictError("cash movement id already exists")
            source_key = (movement.source, movement.source_reference)
            duplicate = self._source_index.get(source_key)
            if duplicate:
                existing = self._movements[duplicate]
                if existing == movement:
                    return existing
                raise StorageConflictError("source reference already has a movement")
            account = self.require_account(movement.account_id)
            if account.status != AccountStatus.ACTIVE:
                raise ValidationError("cash movements require an active account")
            projected = self.balance(account.account_id, as_of=movement.booked_at).booked + movement.amount
            if projected < -account.overdraft_limit:
                raise ValidationError("cash movement exceeds available overdraft")
            self._movements[movement.movement_id] = movement
            self._source_index[source_key] = movement.movement_id
            self._account_movements[account.account_id].append(movement.movement_id)
            return movement

    def reserve(self, reservation: CashReservation) -> CashReservation:
        with self._lock:
            existing = self._reservations.get(reservation.reservation_id)
            if existing is not None:
                if existing == reservation:
                    return existing
                raise StorageConflictError("reservation id already exists")
            reference_key = (reservation.account_id, reservation.reference)
            duplicate = self._reference_reservations.get(reference_key)
            if duplicate:
                return self._reservations[duplicate]
            account = self.require_account(reservation.account_id)
            if account.status != AccountStatus.ACTIVE:
                raise ValidationError("reservations require an active account")
            available = self.balance(account.account_id, as_of=reservation.created_at).available
            if reservation.amount > available + account.overdraft_limit:
                raise ValidationError("reservation exceeds available cash")
            self._reservations[reservation.reservation_id] = reservation
            self._reference_reservations[reference_key] = reservation.reservation_id
            return reservation

    def consume_reservation(self, reservation_id: str, amount: object, *, at: datetime, expected_version: int) -> CashReservation:
        value = Decimal(str(amount))
        if value <= 0:
            raise ValidationError("consumption amount must be positive")
        instant = utc(at, "consumption time")
        with self._lock:
            current = self._reservations.get(text(reservation_id, "reservation id"))
            if current is None:
                raise NotFoundError("cash reservation does not exist")
            if current.version != expected_version:
                raise StorageConflictError("reservation version does not match")
            if not current.active_at(instant) or value > current.remaining:
                raise ValidationError("reservation cannot cover consumption")
            changed = replace(current, consumed_amount=current.consumed_amount + value, released_at=instant if value == current.remaining else None, version=current.version + 1)
            self._reservations[current.reservation_id] = changed
            return changed

    def release_reservation(self, reservation_id: str, *, at: datetime) -> CashReservation:
        instant = utc(at, "release time")
        with self._lock:
            current = self._reservations.get(text(reservation_id, "reservation id"))
            if current is None:
                raise NotFoundError("cash reservation does not exist")
            if current.released_at is not None:
                return current
            changed = replace(current, released_at=instant, version=current.version + 1)
            self._reservations[current.reservation_id] = changed
            return changed

    def expire_reservations(self, at: datetime) -> Tuple[CashReservation, ...]:
        instant = utc(at, "expiry time")
        changed = []
        with self._lock:
            for reservation_id, current in list(self._reservations.items()):
                if current.released_at is None and current.expires_at <= instant:
                    value = replace(current, released_at=instant, version=current.version + 1)
                    self._reservations[reservation_id] = value
                    changed.append(value)
        return tuple(sorted(changed, key=lambda item: item.expires_at))

    def balance(self, account_id: str, *, as_of: datetime) -> AccountBalance:
        instant = utc(as_of, "balance time")
        with self._lock:
            self.require_account(account_id)
            movements = [self._movements[item] for item in self._account_movements.get(account_id, ()) if self._movements[item].booked_at <= instant]
            booked = sum((item.amount for item in movements), Decimal("0"))
            valued = sum((item.amount for item in movements if item.value_date <= instant.date()), Decimal("0"))
            reserved = sum((item.remaining for item in self._reservations.values() if item.account_id == account_id and item.active_at(instant)), Decimal("0"))
            return AccountBalance(account_id, booked, valued, reserved, booked - reserved, instant)

    def movements(self, account_id: str, *, from_time: Optional[datetime] = None, to_time: Optional[datetime] = None) -> Tuple[CashMovement, ...]:
        with self._lock:
            values = [self._movements[item] for item in self._account_movements.get(account_id, ())]
            values = [item for item in values if (from_time is None or item.booked_at >= from_time) and (to_time is None or item.booked_at < to_time)]
            return tuple(sorted(values, key=lambda item: (item.booked_at, item.movement_id)))

    def require_account(self, account_id: str) -> CustodyAccount:
        value = self._accounts.get(text(account_id, "account id"))
        if value is None:
            raise NotFoundError("custody account does not exist")
        return value
