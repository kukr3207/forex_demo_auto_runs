"""Legally enforceable netting sets and collateralized exposures."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Dict, Iterable, Mapping, Optional, Sequence, Tuple

from forex_monitor.errors import StorageConflictError, ValidationError
from forex_monitor.models import as_decimal, ensure_utc


@dataclass(frozen=True)
class NettingAgreement:
    agreement_id: str
    counterparty_id: str
    agreement_type: str
    currency: str
    effective_date: date
    threshold: Decimal = Decimal("0")
    minimum_transfer_amount: Decimal = Decimal("0")
    independent_amount: Decimal = Decimal("0")
    netting_enforceable: bool = True

    def __post_init__(self) -> None:
        if not self.agreement_id.strip() or not self.counterparty_id.strip():
            raise ValidationError("agreement id and counterparty cannot be blank")
        currency = self.currency.strip().upper()
        if len(currency) != 3 or not currency.isalpha():
            raise ValidationError("agreement currency must use an alpha-3 code")
        values = {
            "threshold": as_decimal(self.threshold, "agreement threshold"),
            "minimum_transfer_amount": as_decimal(self.minimum_transfer_amount, "minimum transfer amount"),
            "independent_amount": as_decimal(self.independent_amount, "independent amount"),
        }
        if any(value < 0 for value in values.values()):
            raise ValidationError("agreement monetary terms cannot be negative")
        object.__setattr__(self, "agreement_id", self.agreement_id.strip())
        object.__setattr__(self, "counterparty_id", self.counterparty_id.strip())
        object.__setattr__(self, "agreement_type", self.agreement_type.strip().upper())
        object.__setattr__(self, "currency", currency)
        for name, value in values.items():
            object.__setattr__(self, name, value)


@dataclass(frozen=True)
class MarkToMarket:
    trade_id: str
    counterparty_id: str
    agreement_id: Optional[str]
    currency: str
    value: Decimal
    valued_at: datetime
    maturity_date: date
    product: str

    def __post_init__(self) -> None:
        if not self.trade_id.strip() or not self.counterparty_id.strip() or not self.product.strip():
            raise ValidationError("trade id, counterparty, and product cannot be blank")
        currency = self.currency.strip().upper()
        if len(currency) != 3:
            raise ValidationError("mark-to-market currency must use an alpha-3 code")
        valued_at = ensure_utc(self.valued_at, "valuation time")
        if self.maturity_date < valued_at.date():
            raise ValidationError("trade maturity precedes valuation date")
        object.__setattr__(self, "trade_id", self.trade_id.strip())
        object.__setattr__(self, "counterparty_id", self.counterparty_id.strip())
        object.__setattr__(self, "agreement_id", self.agreement_id.strip() if self.agreement_id else None)
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "value", as_decimal(self.value, "mark-to-market value"))
        object.__setattr__(self, "valued_at", valued_at)
        object.__setattr__(self, "product", self.product.strip().lower())


@dataclass(frozen=True)
class ExposureProfile:
    counterparty_id: str
    currency: str
    valued_at: datetime
    gross_positive: Decimal
    gross_negative: Decimal
    net_market_value: Decimal
    collateral: Decimal
    current_exposure: Decimal
    independent_amount: Decimal
    unsecured_exposure: Decimal
    trade_count: int
    agreement_count: int


class CreditExposureEngine:
    def __init__(self, reporting_currency: str = "USD") -> None:
        self.reporting_currency = reporting_currency.strip().upper()
        if len(self.reporting_currency) != 3:
            raise ValidationError("reporting currency must use an alpha-3 code")
        self._agreements: Dict[str, NettingAgreement] = {}
        self._marks: Dict[str, MarkToMarket] = {}
        self._collateral: Dict[Tuple[str, str], Decimal] = {}

    def add_agreement(self, agreement: NettingAgreement) -> NettingAgreement:
        existing = self._agreements.get(agreement.agreement_id)
        if existing is not None:
            if existing == agreement:
                return existing
            raise StorageConflictError("netting agreement id already exists")
        self._agreements[agreement.agreement_id] = agreement
        return agreement

    def value(self, mark: MarkToMarket) -> MarkToMarket:
        if mark.agreement_id:
            agreement = self._agreements.get(mark.agreement_id)
            if agreement is None:
                raise ValidationError("mark references an unknown netting agreement")
            if agreement.counterparty_id != mark.counterparty_id:
                raise ValidationError("mark counterparty differs from netting agreement")
        existing = self._marks.get(mark.trade_id)
        if existing is not None and existing.valued_at > mark.valued_at:
            raise StorageConflictError("mark-to-market cannot move backward in time")
        if existing == mark:
            return existing
        self._marks[mark.trade_id] = mark
        return mark

    def set_collateral(self, counterparty_id: str, currency: str, amount: object) -> Decimal:
        parsed = as_decimal(amount, "collateral amount")
        if parsed < 0:
            raise ValidationError("collateral amount cannot be negative")
        self._collateral[(counterparty_id.strip(), currency.strip().upper())] = parsed
        return parsed

    def profile(
        self,
        counterparty_id: str,
        valued_at: datetime,
        rates: Mapping[str, object],
    ) -> ExposureProfile:
        boundary = ensure_utc(valued_at, "exposure time")
        marks = tuple(
            mark for mark in self._marks.values()
            if mark.counterparty_id == counterparty_id and mark.valued_at <= boundary
        )
        latest: Dict[str, MarkToMarket] = {}
        for mark in marks:
            current = latest.get(mark.trade_id)
            if current is None or mark.valued_at > current.valued_at:
                latest[mark.trade_id] = mark
        agreements = {
            agreement.agreement_id: agreement
            for agreement in self._agreements.values()
            if agreement.counterparty_id == counterparty_id
        }
        converted = tuple((mark, self._convert(mark.value, mark.currency, rates)) for mark in latest.values())
        gross_positive = sum((max(Decimal("0"), value) for _, value in converted), Decimal("0"))
        gross_negative = sum((min(Decimal("0"), value) for _, value in converted), Decimal("0"))
        netted_values: Dict[str, Decimal] = {}
        independent = Decimal("0")
        independent_agreements: set[str] = set()
        for mark, value in converted:
            agreement = agreements.get(mark.agreement_id or "")
            if agreement and agreement.netting_enforceable:
                key = agreement.agreement_id
                if agreement.agreement_id not in independent_agreements:
                    independent += self._convert(agreement.independent_amount, agreement.currency, rates)
                    independent_agreements.add(agreement.agreement_id)
            else:
                key = "trade:" + mark.trade_id
            netted_values[key] = netted_values.get(key, Decimal("0")) + value
        current_exposure = sum((max(Decimal("0"), value) for value in netted_values.values()), Decimal("0"))
        collateral = sum(
            (self._convert(amount, currency, rates) for (party, currency), amount in self._collateral.items() if party == counterparty_id),
            Decimal("0"),
        )
        unsecured = max(Decimal("0"), current_exposure + independent - collateral)
        return ExposureProfile(
            counterparty_id,
            self.reporting_currency,
            boundary,
            gross_positive,
            gross_negative,
            sum((value for _, value in converted), Decimal("0")),
            collateral,
            current_exposure,
            independent,
            unsecured,
            len(latest),
            len(agreements),
        )

    def _convert(self, amount: Decimal, currency: str, rates: Mapping[str, object]) -> Decimal:
        if currency == self.reporting_currency:
            return amount
        direct = currency + self.reporting_currency
        inverse = self.reporting_currency + currency
        if direct in rates:
            return amount * as_decimal(rates[direct], "exposure conversion rate", positive=True)
        if inverse in rates:
            return amount / as_decimal(rates[inverse], "exposure conversion rate", positive=True)
        raise ValidationError("missing exposure conversion rate for " + currency)
