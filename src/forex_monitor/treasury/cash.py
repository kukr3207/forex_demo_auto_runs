"""Multi-currency double-entry cash ledger."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Dict, Mapping, Optional, Sequence, Tuple

from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError
from forex_monitor.models import as_decimal, ensure_utc


def _currency(value: str) -> str:
    result = value.strip().upper()
    if len(result) != 3 or not result.isalpha():
        raise ValidationError("currency must contain three letters")
    return result


class MovementKind(str, Enum):
    TRADE = "trade"
    FUNDING = "funding"
    FEE = "fee"
    COLLATERAL = "collateral"
    TRANSFER = "transfer"
    ADJUSTMENT = "adjustment"


@dataclass(frozen=True)
class CashAccount:
    account_id: str
    currency: str
    owner: str
    minimum_balance: Decimal = Decimal("0")
    overdraft_limit: Decimal = Decimal("0")
    active: bool = True

    def __post_init__(self) -> None:
        identifier = self.account_id.strip()
        owner = self.owner.strip()
        if not identifier or not owner:
            raise ValidationError("cash account id and owner cannot be blank")
        minimum = as_decimal(self.minimum_balance, "minimum balance")
        overdraft = as_decimal(self.overdraft_limit, "overdraft limit")
        if overdraft < 0:
            raise ValidationError("overdraft limit cannot be negative")
        object.__setattr__(self, "account_id", identifier)
        object.__setattr__(self, "owner", owner)
        object.__setattr__(self, "currency", _currency(self.currency))
        object.__setattr__(self, "minimum_balance", minimum)
        object.__setattr__(self, "overdraft_limit", overdraft)


@dataclass(frozen=True)
class CashMovement:
    movement_id: str
    debit_account: str
    credit_account: str
    amount: Decimal
    currency: str
    value_date: date
    booked_at: datetime
    kind: MovementKind
    reference: str

    def __post_init__(self) -> None:
        identifier = self.movement_id.strip()
        debit = self.debit_account.strip()
        credit = self.credit_account.strip()
        reference = self.reference.strip()
        if not identifier or not debit or not credit or not reference:
            raise ValidationError("movement identifiers and reference cannot be blank")
        if debit == credit:
            raise ValidationError("cash movement accounts must differ")
        object.__setattr__(self, "movement_id", identifier)
        object.__setattr__(self, "debit_account", debit)
        object.__setattr__(self, "credit_account", credit)
        object.__setattr__(self, "amount", as_decimal(self.amount, "cash amount", positive=True))
        object.__setattr__(self, "currency", _currency(self.currency))
        object.__setattr__(self, "booked_at", ensure_utc(self.booked_at, "booking time"))
        object.__setattr__(self, "kind", self.kind if isinstance(self.kind, MovementKind) else MovementKind(self.kind))
        object.__setattr__(self, "reference", reference)


@dataclass(frozen=True)
class BalanceSnapshot:
    account_id: str
    currency: str
    value_date: date
    booked_balance: Decimal
    available_balance: Decimal
    movement_count: int


class CashLedger:
    def __init__(self) -> None:
        self._accounts: Dict[str, CashAccount] = {}
        self._movements: Dict[str, CashMovement] = {}
        self._balances: Dict[str, Decimal] = {}

    def open(self, account: CashAccount, opening_balance: object = Decimal("0")) -> None:
        if account.account_id in self._accounts:
            raise StorageConflictError("cash account already exists")
        balance = as_decimal(opening_balance, "opening balance")
        if balance < -account.overdraft_limit:
            raise ValidationError("opening balance exceeds the overdraft limit")
        self._accounts[account.account_id] = account
        self._balances[account.account_id] = balance

    def account(self, account_id: str) -> CashAccount:
        try:
            return self._accounts[account_id]
        except KeyError as exc:
            raise NotFoundError("cash account was not found") from exc

    def balance(self, account_id: str) -> Decimal:
        self.account(account_id)
        return self._balances[account_id]

    def book(self, movement: CashMovement) -> CashMovement:
        existing = self._movements.get(movement.movement_id)
        if existing is not None:
            if existing == movement:
                return existing
            raise StorageConflictError("cash movement id was reused with different data")
        debit = self.account(movement.debit_account)
        credit = self.account(movement.credit_account)
        if debit.currency != movement.currency or credit.currency != movement.currency:
            raise ValidationError("cash movement currency must match both accounts")
        if not debit.active or not credit.active:
            raise ValidationError("cash movement references an inactive account")
        debit_balance = self._balances[debit.account_id] - movement.amount
        if debit_balance < -debit.overdraft_limit:
            raise ValidationError("cash movement exceeds the debit account overdraft")
        self._balances[debit.account_id] = debit_balance
        self._balances[credit.account_id] += movement.amount
        self._movements[movement.movement_id] = movement
        return movement

    def book_batch(self, movements: Sequence[CashMovement]) -> Tuple[CashMovement, ...]:
        if len({item.movement_id for item in movements}) != len(movements):
            raise ValidationError("cash batch contains repeated movement ids")
        balances = dict(self._balances)
        new = []
        for movement in movements:
            existing = self._movements.get(movement.movement_id)
            if existing is not None:
                if existing != movement:
                    raise StorageConflictError("cash movement id conflicts with stored data")
                continue
            debit = self.account(movement.debit_account)
            credit = self.account(movement.credit_account)
            if debit.currency != movement.currency or credit.currency != movement.currency:
                raise ValidationError("cash movement currency must match both accounts")
            next_balance = balances[debit.account_id] - movement.amount
            if next_balance < -debit.overdraft_limit:
                raise ValidationError("cash batch exceeds an overdraft limit")
            balances[debit.account_id] = next_balance
            balances[credit.account_id] += movement.amount
            new.append(movement)
        self._balances = balances
        for movement in new:
            self._movements[movement.movement_id] = movement
        return tuple(self._movements[item.movement_id] for item in movements)

    def movements(self, account_id: str, *, through: Optional[date] = None) -> Tuple[CashMovement, ...]:
        self.account(account_id)
        return tuple(sorted(
            (
                item for item in self._movements.values()
                if account_id in {item.debit_account, item.credit_account}
                and (through is None or item.value_date <= through)
            ),
            key=lambda item: (item.value_date, item.booked_at, item.movement_id),
        ))

    def snapshot(self, account_id: str, value_date: date) -> BalanceSnapshot:
        account = self.account(account_id)
        balance = Decimal("0")
        count = 0
        for movement in self.movements(account_id, through=value_date):
            count += 1
            balance += movement.amount if movement.credit_account == account_id else -movement.amount
        available = balance + account.overdraft_limit
        return BalanceSnapshot(account_id, account.currency, value_date, balance, available, count)

    def trial_balance(self) -> Mapping[str, Decimal]:
        result: Dict[str, Decimal] = {}
        for account_id, balance in self._balances.items():
            currency = self._accounts[account_id].currency
            result[currency] = result.get(currency, Decimal("0")) + balance
        return result
