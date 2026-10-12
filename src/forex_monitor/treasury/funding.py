"""Deposit and borrowing quotes for short-term funding decisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Dict, Mapping, Optional, Sequence, Tuple

from forex_monitor.errors import ValidationError
from forex_monitor.models import as_decimal
from forex_monitor.pricing.day_count import DayCount, year_fraction
from forex_monitor.treasury.cash import _currency


class FundingSide(str, Enum):
    BORROW = "borrow"
    LEND = "lend"


@dataclass(frozen=True)
class FundingInstrument:
    instrument_id: str
    currency: str
    start_date: date
    maturity_date: date
    principal: Decimal
    side: FundingSide
    day_count: DayCount = DayCount.ACT_360

    def __post_init__(self) -> None:
        identifier = self.instrument_id.strip()
        if not identifier:
            raise ValidationError("funding instrument id cannot be blank")
        if self.maturity_date <= self.start_date:
            raise ValidationError("funding maturity must follow start date")
        object.__setattr__(self, "instrument_id", identifier)
        object.__setattr__(self, "currency", _currency(self.currency))
        object.__setattr__(self, "principal", as_decimal(self.principal, "funding principal", positive=True))
        object.__setattr__(self, "side", self.side if isinstance(self.side, FundingSide) else FundingSide(self.side))
        object.__setattr__(self, "day_count", DayCount.parse(self.day_count))

    @property
    def accrual_fraction(self) -> Decimal:
        return year_fraction(self.start_date, self.maturity_date, self.day_count)


@dataclass(frozen=True)
class FundingQuote:
    provider: str
    currency: str
    start_date: date
    maturity_date: date
    borrow_rate: Decimal
    lend_rate: Decimal
    capacity: Decimal

    def __post_init__(self) -> None:
        provider = self.provider.strip()
        if not provider:
            raise ValidationError("funding provider cannot be blank")
        borrow = as_decimal(self.borrow_rate, "borrow rate")
        lend = as_decimal(self.lend_rate, "lend rate")
        if lend > borrow:
            raise ValidationError("funding lend rate cannot exceed borrow rate")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "currency", _currency(self.currency))
        object.__setattr__(self, "borrow_rate", borrow)
        object.__setattr__(self, "lend_rate", lend)
        object.__setattr__(self, "capacity", as_decimal(self.capacity, "funding capacity", positive=True))


@dataclass(frozen=True)
class FundingDecision:
    instrument_id: str
    provider: str
    rate: Decimal
    principal: Decimal
    interest: Decimal
    maturity_amount: Decimal


class FundingDesk:
    def __init__(self) -> None:
        self._quotes: Dict[Tuple[str, date, date, str], FundingQuote] = {}

    def quote(self, value: FundingQuote) -> None:
        key = (value.currency, value.start_date, value.maturity_date, value.provider)
        self._quotes[key] = value

    def candidates(self, instrument: FundingInstrument) -> Tuple[FundingQuote, ...]:
        result = [
            quote for (currency, start, maturity, _), quote in self._quotes.items()
            if currency == instrument.currency
            and start == instrument.start_date
            and maturity == instrument.maturity_date
            and quote.capacity >= instrument.principal
        ]
        if instrument.side is FundingSide.BORROW:
            result.sort(key=lambda item: (item.borrow_rate, item.provider))
        else:
            result.sort(key=lambda item: (-item.lend_rate, item.provider))
        return tuple(result)

    def best(self, instrument: FundingInstrument) -> FundingDecision:
        candidates = self.candidates(instrument)
        if not candidates:
            raise ValidationError("no funding quote can satisfy the instrument")
        quote = candidates[0]
        rate = quote.borrow_rate if instrument.side is FundingSide.BORROW else quote.lend_rate
        interest = instrument.principal * rate * instrument.accrual_fraction
        signed_interest = interest if instrument.side is FundingSide.BORROW else -interest
        return FundingDecision(
            instrument.instrument_id,
            quote.provider,
            rate,
            instrument.principal,
            signed_interest,
            instrument.principal + signed_interest,
        )

    def blended(self, instrument: FundingInstrument, maximum_providers: int = 5) -> Tuple[FundingDecision, ...]:
        if maximum_providers <= 0:
            raise ValidationError("maximum providers must be positive")
        remaining = instrument.principal
        quotes = [
            quote
            for (currency, start, maturity, _), quote in self._quotes.items()
            if currency == instrument.currency
            and start == instrument.start_date
            and maturity == instrument.maturity_date
        ]
        if instrument.side is FundingSide.BORROW:
            quotes.sort(key=lambda item: (item.borrow_rate, item.provider))
        else:
            quotes.sort(key=lambda item: (-item.lend_rate, item.provider))
        result = []
        for quote in quotes[:maximum_providers]:
            amount = min(remaining, quote.capacity)
            rate = quote.borrow_rate if instrument.side is FundingSide.BORROW else quote.lend_rate
            interest = amount * rate * instrument.accrual_fraction
            if instrument.side is FundingSide.LEND:
                interest = -interest
            result.append(FundingDecision(instrument.instrument_id, quote.provider, rate, amount, interest, amount + interest))
            remaining -= amount
            if remaining == 0:
                break
        if remaining:
            raise ValidationError("funding capacity is insufficient across selected providers")
        return tuple(result)
