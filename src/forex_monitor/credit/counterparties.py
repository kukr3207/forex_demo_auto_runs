"""Counterparty master data and effective-dated credit limits."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Dict, Mapping, Optional, Tuple

from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError
from forex_monitor.models import as_decimal, ensure_utc


class CounterpartyState(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    RESTRICTED = "restricted"
    SUSPENDED = "suspended"
    CLOSED = "closed"


@dataclass(frozen=True)
class Counterparty:
    counterparty_id: str
    legal_name: str
    lei: str
    country: str
    rating: str
    state: CounterpartyState = CounterpartyState.PENDING
    parent_id: Optional[str] = None
    sector: str = "financial"
    onboarding_date: Optional[date] = None
    version: int = 1

    def __post_init__(self) -> None:
        identifier = self.counterparty_id.strip()
        name = self.legal_name.strip()
        lei = self.lei.strip().upper()
        country = self.country.strip().upper()
        rating = self.rating.strip().upper()
        if not identifier or not name or not rating:
            raise ValidationError("counterparty id, legal name, and rating cannot be blank")
        if len(lei) != 20 or not lei.isalnum():
            raise ValidationError("counterparty LEI must contain 20 alphanumeric characters")
        if len(country) != 2 or not country.isalpha():
            raise ValidationError("counterparty country must use an alpha-2 code")
        if self.version < 1:
            raise ValidationError("counterparty version must be positive")
        object.__setattr__(self, "counterparty_id", identifier)
        object.__setattr__(self, "legal_name", name)
        object.__setattr__(self, "lei", lei)
        object.__setattr__(self, "country", country)
        object.__setattr__(self, "rating", rating)
        object.__setattr__(self, "state", self.state if isinstance(self.state, CounterpartyState) else CounterpartyState(self.state))
        object.__setattr__(self, "parent_id", self.parent_id.strip() if self.parent_id else None)
        object.__setattr__(self, "sector", self.sector.strip().lower())


@dataclass(frozen=True)
class CreditLimit:
    limit_id: str
    counterparty_id: str
    currency: str
    amount: Decimal
    effective_from: datetime
    effective_until: Optional[datetime] = None
    product: Optional[str] = None
    tenor_days: Optional[int] = None
    approved_by: str = ""

    def __post_init__(self) -> None:
        identifier = self.limit_id.strip()
        counterparty = self.counterparty_id.strip()
        currency = self.currency.strip().upper()
        approver = self.approved_by.strip()
        if not identifier or not counterparty or not approver:
            raise ValidationError("limit id, counterparty, and approver cannot be blank")
        if len(currency) != 3 or not currency.isalpha():
            raise ValidationError("credit limit currency must use an alpha-3 code")
        effective_from = ensure_utc(self.effective_from, "limit start")
        effective_until = ensure_utc(self.effective_until, "limit end") if self.effective_until else None
        if effective_until is not None and effective_until <= effective_from:
            raise ValidationError("credit limit end must follow its start")
        if self.tenor_days is not None and self.tenor_days < 1:
            raise ValidationError("credit limit tenor must be positive")
        object.__setattr__(self, "limit_id", identifier)
        object.__setattr__(self, "counterparty_id", counterparty)
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "amount", as_decimal(self.amount, "credit limit", positive=True))
        object.__setattr__(self, "effective_from", effective_from)
        object.__setattr__(self, "effective_until", effective_until)
        object.__setattr__(self, "product", self.product.strip().lower() if self.product else None)
        object.__setattr__(self, "approved_by", approver)

    def applies(self, at: datetime, product: Optional[str], tenor_days: Optional[int]) -> bool:
        boundary = ensure_utc(at, "limit evaluation time")
        if boundary < self.effective_from or (self.effective_until and boundary >= self.effective_until):
            return False
        if self.product and self.product != (product or "").strip().lower():
            return False
        return not (self.tenor_days is not None and (tenor_days is None or tenor_days > self.tenor_days))


class CounterpartyBook:
    _TRANSITIONS = {
        CounterpartyState.PENDING: {CounterpartyState.ACTIVE, CounterpartyState.CLOSED},
        CounterpartyState.ACTIVE: {CounterpartyState.RESTRICTED, CounterpartyState.SUSPENDED, CounterpartyState.CLOSED},
        CounterpartyState.RESTRICTED: {CounterpartyState.ACTIVE, CounterpartyState.SUSPENDED, CounterpartyState.CLOSED},
        CounterpartyState.SUSPENDED: {CounterpartyState.ACTIVE, CounterpartyState.CLOSED},
        CounterpartyState.CLOSED: set(),
    }

    def __init__(self) -> None:
        self._counterparties: Dict[str, Counterparty] = {}
        self._leis: Dict[str, str] = {}
        self._limits: Dict[str, CreditLimit] = {}

    def register(self, counterparty: Counterparty) -> Counterparty:
        existing = self._counterparties.get(counterparty.counterparty_id)
        if existing is not None:
            if existing == counterparty:
                return existing
            raise StorageConflictError("counterparty id already exists")
        lei_owner = self._leis.get(counterparty.lei)
        if lei_owner is not None:
            raise StorageConflictError("counterparty LEI already exists")
        if counterparty.parent_id and counterparty.parent_id not in self._counterparties:
            raise ValidationError("counterparty parent does not exist")
        self._counterparties[counterparty.counterparty_id] = counterparty
        self._leis[counterparty.lei] = counterparty.counterparty_id
        return counterparty

    def get(self, counterparty_id: str) -> Counterparty:
        try:
            return self._counterparties[counterparty_id]
        except KeyError as exc:
            raise NotFoundError("counterparty was not found") from exc

    def transition(
        self,
        counterparty_id: str,
        expected_version: int,
        state: CounterpartyState,
    ) -> Counterparty:
        current = self.get(counterparty_id)
        if current.version != expected_version:
            raise StorageConflictError("counterparty version does not match")
        target = state if isinstance(state, CounterpartyState) else CounterpartyState(state)
        if target == current.state:
            return current
        if target not in self._TRANSITIONS[current.state]:
            raise ValidationError("counterparty state transition is not allowed")
        changed = replace(current, state=target, version=current.version + 1)
        self._counterparties[counterparty_id] = changed
        return changed

    def add_limit(self, limit: CreditLimit) -> CreditLimit:
        self.get(limit.counterparty_id)
        existing = self._limits.get(limit.limit_id)
        if existing is not None:
            if existing == limit:
                return existing
            raise StorageConflictError("credit limit id already exists")
        for current in self._limits.values():
            if (
                current.counterparty_id == limit.counterparty_id
                and current.currency == limit.currency
                and current.product == limit.product
                and current.tenor_days == limit.tenor_days
                and _overlaps(current, limit)
            ):
                raise ValidationError("credit limit overlaps an existing limit")
        self._limits[limit.limit_id] = limit
        return limit

    def applicable_limits(
        self,
        counterparty_id: str,
        currency: str,
        at: datetime,
        product: Optional[str] = None,
        tenor_days: Optional[int] = None,
    ) -> Tuple[CreditLimit, ...]:
        self.get(counterparty_id)
        normalized_currency = currency.strip().upper()
        return tuple(sorted(
            (
                limit for limit in self._limits.values()
                if limit.counterparty_id == counterparty_id
                and limit.currency == normalized_currency
                and limit.applies(at, product, tenor_days)
            ),
            key=lambda item: (item.product is not None, item.tenor_days or 10**9, item.amount),
            reverse=True,
        ))

    def available_limit(
        self,
        counterparty_id: str,
        currency: str,
        current_exposure: object,
        at: datetime,
        product: Optional[str] = None,
        tenor_days: Optional[int] = None,
    ) -> Decimal:
        counterparty = self.get(counterparty_id)
        if counterparty.state is not CounterpartyState.ACTIVE:
            return Decimal("0")
        limits = self.applicable_limits(counterparty_id, currency, at, product, tenor_days)
        if not limits:
            return Decimal("0")
        limit_amount = min(item.amount for item in limits)
        exposure = as_decimal(current_exposure, "current credit exposure")
        return max(Decimal("0"), limit_amount - exposure)

    def group_members(self, parent_id: str) -> Tuple[Counterparty, ...]:
        parent = self.get(parent_id)
        members = [parent]
        members.extend(item for item in self._counterparties.values() if item.parent_id == parent_id)
        return tuple(sorted(members, key=lambda item: item.counterparty_id))


def _overlaps(left: CreditLimit, right: CreditLimit) -> bool:
    left_end = left.effective_until or datetime.max.replace(tzinfo=left.effective_from.tzinfo)
    right_end = right.effective_until or datetime.max.replace(tzinfo=right.effective_from.tzinfo)
    return left.effective_from < right_end and right.effective_from < left_end

