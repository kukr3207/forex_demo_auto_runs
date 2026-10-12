"""Collateral eligibility, haircuts, concentration, and margin calls."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Dict, Mapping, Optional, Sequence, Tuple

from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError
from forex_monitor.models import as_decimal, ensure_utc
from forex_monitor.treasury.cash import _currency


class CollateralType(str, Enum):
    CASH = "cash"
    GOVERNMENT_BOND = "government_bond"
    CORPORATE_BOND = "corporate_bond"
    EQUITY = "equity"
    LETTER_OF_CREDIT = "letter_of_credit"


class MarginCallState(str, Enum):
    OPEN = "open"
    PARTIALLY_SATISFIED = "partially_satisfied"
    SATISFIED = "satisfied"
    DISPUTED = "disputed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class CollateralAsset:
    asset_id: str
    asset_type: CollateralType
    currency: str
    market_value: Decimal
    haircut: Decimal
    issuer: str
    maturity_date: Optional[date] = None
    rating: Optional[str] = None
    pledged_to: Optional[str] = None

    def __post_init__(self) -> None:
        identifier = self.asset_id.strip()
        issuer = self.issuer.strip()
        if not identifier or not issuer:
            raise ValidationError("collateral asset id and issuer cannot be blank")
        haircut = as_decimal(self.haircut, "collateral haircut")
        if haircut < 0 or haircut >= 1:
            raise ValidationError("collateral haircut must be between zero and one")
        object.__setattr__(self, "asset_id", identifier)
        object.__setattr__(self, "asset_type", self.asset_type if isinstance(self.asset_type, CollateralType) else CollateralType(self.asset_type))
        object.__setattr__(self, "currency", _currency(self.currency))
        object.__setattr__(self, "market_value", as_decimal(self.market_value, "market value", positive=True))
        object.__setattr__(self, "haircut", haircut)
        object.__setattr__(self, "issuer", issuer)

    @property
    def eligible_value(self) -> Decimal:
        return self.market_value * (Decimal("1") - self.haircut)


@dataclass(frozen=True)
class MarginCall:
    call_id: str
    counterparty: str
    currency: str
    required_amount: Decimal
    due_at: datetime
    state: MarginCallState = MarginCallState.OPEN
    satisfied_amount: Decimal = Decimal("0")
    dispute_reason: Optional[str] = None

    def __post_init__(self) -> None:
        identifier = self.call_id.strip()
        counterparty = self.counterparty.strip()
        if not identifier or not counterparty:
            raise ValidationError("margin call id and counterparty cannot be blank")
        required = as_decimal(self.required_amount, "required margin", positive=True)
        satisfied = as_decimal(self.satisfied_amount, "satisfied margin")
        if satisfied < 0 or satisfied > required:
            raise ValidationError("satisfied margin must be within the required amount")
        state = self.state if isinstance(self.state, MarginCallState) else MarginCallState(self.state)
        if state is MarginCallState.SATISFIED and satisfied != required:
            raise ValidationError("satisfied call state requires the full amount")
        object.__setattr__(self, "call_id", identifier)
        object.__setattr__(self, "counterparty", counterparty)
        object.__setattr__(self, "currency", _currency(self.currency))
        object.__setattr__(self, "required_amount", required)
        object.__setattr__(self, "satisfied_amount", satisfied)
        object.__setattr__(self, "due_at", ensure_utc(self.due_at, "margin due time"))
        object.__setattr__(self, "state", state)

    @property
    def outstanding(self) -> Decimal:
        return self.required_amount - self.satisfied_amount


class CollateralPool:
    def __init__(self, reporting_currency: str = "USD") -> None:
        self.reporting_currency = _currency(reporting_currency)
        self._assets: Dict[str, CollateralAsset] = {}
        self._calls: Dict[str, MarginCall] = {}

    def add(self, asset: CollateralAsset) -> None:
        if asset.asset_id in self._assets:
            raise StorageConflictError("collateral asset already exists")
        self._assets[asset.asset_id] = asset

    def asset(self, asset_id: str) -> CollateralAsset:
        try:
            return self._assets[asset_id]
        except KeyError as exc:
            raise NotFoundError("collateral asset was not found") from exc

    def revalue(self, asset_id: str, market_value: object) -> CollateralAsset:
        asset = self.asset(asset_id)
        changed = replace(asset, market_value=as_decimal(market_value, "market value", positive=True))
        self._assets[asset_id] = changed
        return changed

    def pledge(self, asset_id: str, counterparty: str) -> CollateralAsset:
        asset = self.asset(asset_id)
        if asset.pledged_to:
            raise ValidationError("collateral asset is already pledged")
        changed = replace(asset, pledged_to=counterparty.strip())
        self._assets[asset_id] = changed
        return changed

    def release(self, asset_id: str, counterparty: str) -> CollateralAsset:
        asset = self.asset(asset_id)
        if asset.pledged_to != counterparty.strip():
            raise ValidationError("collateral asset is not pledged to this counterparty")
        changed = replace(asset, pledged_to=None)
        self._assets[asset_id] = changed
        return changed

    def open_call(self, call: MarginCall) -> MarginCall:
        existing = self._calls.get(call.call_id)
        if existing is not None:
            if existing == call:
                return existing
            raise StorageConflictError("margin call id already exists")
        self._calls[call.call_id] = call
        return call

    def satisfy(self, call_id: str, amount: object) -> MarginCall:
        try:
            call = self._calls[call_id]
        except KeyError as exc:
            raise NotFoundError("margin call was not found") from exc
        if call.state in {MarginCallState.SATISFIED, MarginCallState.CANCELLED}:
            raise ValidationError("margin call is no longer open")
        parsed = as_decimal(amount, "margin amount", positive=True)
        if parsed > call.outstanding:
            raise ValidationError("margin amount exceeds the outstanding call")
        satisfied = call.satisfied_amount + parsed
        state = MarginCallState.SATISFIED if satisfied == call.required_amount else MarginCallState.PARTIALLY_SATISFIED
        changed = replace(call, satisfied_amount=satisfied, state=state, dispute_reason=None)
        self._calls[call_id] = changed
        return changed

    def available(self) -> Tuple[CollateralAsset, ...]:
        return tuple(sorted((item for item in self._assets.values() if item.pledged_to is None), key=lambda item: item.asset_id))

    def pledged(self, counterparty: str) -> Tuple[CollateralAsset, ...]:
        return tuple(sorted((item for item in self._assets.values() if item.pledged_to == counterparty), key=lambda item: item.asset_id))

    def eligible_total(self, rates: Mapping[str, object]) -> Decimal:
        total = Decimal("0")
        for asset in self._assets.values():
            value = asset.eligible_value
            if asset.currency != self.reporting_currency:
                pair = asset.currency + self.reporting_currency
                if pair not in rates:
                    raise ValidationError("missing collateral conversion rate for " + pair)
                value *= as_decimal(rates[pair], "collateral conversion rate", positive=True)
            total += value
        return total

    def issuer_concentration(self) -> Mapping[str, Decimal]:
        total = sum((item.eligible_value for item in self._assets.values()), Decimal("0"))
        if total == 0:
            return {}
        values: Dict[str, Decimal] = {}
        for asset in self._assets.values():
            values[asset.issuer] = values.get(asset.issuer, Decimal("0")) + asset.eligible_value
        return {issuer: value / total for issuer, value in values.items()}
