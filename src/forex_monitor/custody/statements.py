"""Custody statement opening, closing, and movement reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from threading import RLock
from typing import Dict, Tuple

from forex_monitor.custody.accounts import CustodyLedger
from forex_monitor.custody.models import CashMovement, utc
from forex_monitor.errors import StorageConflictError, ValidationError


@dataclass(frozen=True)
class CashStatement:
    statement_id: str
    account_id: str
    start: datetime
    end: datetime
    opening_balance: Decimal
    closing_balance: Decimal
    movements: Tuple[CashMovement, ...]
    generated_at: datetime

    @property
    def net_movement(self) -> Decimal:
        return sum((item.amount for item in self.movements), Decimal("0"))

    @property
    def reconciled(self) -> bool:
        return self.opening_balance + self.net_movement == self.closing_balance


class StatementService:
    def __init__(self, ledger: CustodyLedger) -> None:
        self.ledger = ledger
        self._statements: Dict[str, CashStatement] = {}
        self._lock = RLock()

    def generate(self, *, statement_id: str, account_id: str, start: datetime, end: datetime, generated_at: datetime) -> CashStatement:
        start_time = utc(start, "statement start")
        end_time = utc(end, "statement end")
        if end_time <= start_time:
            raise ValidationError("statement end must follow start")
        opening = self.ledger.balance(account_id, as_of=start_time).booked
        closing = self.ledger.balance(account_id, as_of=end_time).booked
        # The opening balance already includes entries booked exactly at start.
        movements = tuple(
            item
            for item in self.ledger.movements(account_id, from_time=start_time, to_time=end_time)
            if item.booked_at > start_time
        )
        statement = CashStatement(statement_id, account_id, start_time, end_time, opening, closing, movements, utc(generated_at, "generated at"))
        if not statement.reconciled:
            raise ValidationError("custody statement does not reconcile")
        with self._lock:
            existing = self._statements.get(statement_id)
            if existing is not None:
                if existing == statement:
                    return existing
                raise StorageConflictError("statement id already exists")
            self._statements[statement_id] = statement
            return statement
