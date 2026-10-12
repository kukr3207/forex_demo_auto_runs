"""Dual-control internal cash transfers with atomic settlement."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from threading import RLock
from typing import Dict, Optional, Tuple

from forex_monitor.custody.accounts import CustodyLedger
from forex_monitor.custody.models import CashMovement, CashReservation, CashTransfer, MovementType, TransferState, text, utc
from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError


class TransferService:
    def __init__(self, ledger: CustodyLedger) -> None:
        self.ledger = ledger
        self._transfers: Dict[str, CashTransfer] = {}
        self._approvals: Dict[str, Tuple[str, ...]] = {}
        self._lock = RLock()

    def create(self, transfer: CashTransfer) -> CashTransfer:
        source = self.ledger.require_account(transfer.source_account_id)
        destination = self.ledger.require_account(transfer.destination_account_id)
        if source.currency != transfer.currency or destination.currency != transfer.currency:
            raise ValidationError("transfer currency does not match custody accounts")
        with self._lock:
            existing = self._transfers.get(transfer.transfer_id)
            if existing is not None:
                if existing == transfer:
                    return existing
                raise StorageConflictError("cash transfer id already exists")
            self._transfers[transfer.transfer_id] = transfer
            self._approvals[transfer.transfer_id] = ()
            return transfer

    def approve(self, transfer_id: str, *, approver: str, required_approvals: int = 1) -> CashTransfer:
        if required_approvals < 1:
            raise ValidationError("required approvals must be positive")
        identity = text(approver, "approver")
        with self._lock:
            current = self._require(transfer_id)
            if current.state not in {TransferState.CREATED, TransferState.APPROVED}:
                raise ValidationError("transfer does not accept approvals")
            if identity == current.requested_by:
                raise ValidationError("transfer requester cannot approve")
            approvals = tuple(sorted(set(self._approvals[transfer_id] + (identity,))))
            self._approvals[transfer_id] = approvals
            if len(approvals) >= required_approvals and current.state == TransferState.CREATED:
                current = replace(current, state=TransferState.APPROVED, version=current.version + 1)
                self._transfers[transfer_id] = current
            return current

    def reserve(self, transfer_id: str, *, at: datetime, ttl: timedelta = timedelta(hours=1), expected_version: int) -> CashTransfer:
        instant = utc(at, "reserve time")
        with self._lock:
            current = self._require(transfer_id)
            self._check_version(current, expected_version)
            if current.state != TransferState.APPROVED:
                raise ValidationError("only approved transfers can reserve cash")
            reservation = self.ledger.reserve(CashReservation("transfer:%s" % current.transfer_id, current.source_account_id, current.amount, "cash transfer", current.transfer_id, instant, instant + ttl))
            changed = replace(current, state=TransferState.RESERVED, reservation_id=reservation.reservation_id, version=current.version + 1)
            self._transfers[current.transfer_id] = changed
            return changed

    def settle(self, transfer_id: str, *, at: datetime, expected_version: int) -> CashTransfer:
        instant = utc(at, "settlement time")
        with self._lock:
            current = self._require(transfer_id)
            self._check_version(current, expected_version)
            if current.state != TransferState.RESERVED or not current.reservation_id:
                raise ValidationError("transfer is not reserved")
            reservation = self.ledger.consume_reservation(current.reservation_id, current.amount, at=instant, expected_version=1)
            debit = CashMovement("%s:out" % current.transfer_id, current.source_account_id, MovementType.TRANSFER_OUT, -current.amount, instant, instant.date(), "transfer", current.transfer_id, "Internal transfer")
            credit = CashMovement("%s:in" % current.transfer_id, current.destination_account_id, MovementType.TRANSFER_IN, current.amount, instant, instant.date(), "transfer", "%s:in" % current.transfer_id, "Internal transfer")
            try:
                self.ledger.post(debit)
                self.ledger.post(credit)
            except Exception:
                raise
            changed = replace(current, state=TransferState.SETTLED, completed_at=instant, version=current.version + 1)
            self._transfers[current.transfer_id] = changed
            return changed

    def cancel(self, transfer_id: str, *, reason: str, at: datetime, expected_version: int) -> CashTransfer:
        instant = utc(at, "cancel time")
        with self._lock:
            current = self._require(transfer_id)
            self._check_version(current, expected_version)
            if current.state not in {TransferState.CREATED, TransferState.APPROVED, TransferState.RESERVED}:
                raise ValidationError("transfer cannot be cancelled")
            if current.reservation_id:
                self.ledger.release_reservation(current.reservation_id, at=instant)
            changed = replace(current, state=TransferState.CANCELLED, failure_reason=text(reason, "cancel reason"), completed_at=instant, version=current.version + 1)
            self._transfers[current.transfer_id] = changed
            return changed

    def transfers(self, *, state: Optional[TransferState] = None) -> Tuple[CashTransfer, ...]:
        with self._lock:
            values = [item for item in self._transfers.values() if state is None or item.state == state]
            return tuple(sorted(values, key=lambda item: (item.requested_at, item.transfer_id)))

    def _require(self, transfer_id: str) -> CashTransfer:
        value = self._transfers.get(text(transfer_id, "transfer id"))
        if value is None:
            raise NotFoundError("cash transfer does not exist")
        return value

    @staticmethod
    def _check_version(transfer: CashTransfer, expected_version: int) -> None:
        if transfer.version != expected_version:
            raise StorageConflictError("cash transfer version does not match")
