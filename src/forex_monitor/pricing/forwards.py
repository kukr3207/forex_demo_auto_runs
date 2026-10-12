"""Deliverable FX-forward contracts and mark-to-market calculations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Mapping, Optional

from forex_monitor.errors import ValidationError
from forex_monitor.models import as_decimal, normalize_symbol
from forex_monitor.pricing.curves import FxForwardCurve


class ForwardSide(str, Enum):
    BUY_BASE = "buy_base"
    SELL_BASE = "sell_base"

    @property
    def sign(self) -> Decimal:
        return Decimal("1") if self is ForwardSide.BUY_BASE else Decimal("-1")


@dataclass(frozen=True)
class ForwardContract:
    contract_id: str
    symbol: str
    side: ForwardSide
    notional: Decimal
    strike: Decimal
    maturity: date
    trade_date: date
    counterparty: Optional[str] = None

    def __post_init__(self) -> None:
        contract_id = self.contract_id.strip()
        if not contract_id:
            raise ValidationError("forward contract id cannot be blank")
        if self.maturity <= self.trade_date:
            raise ValidationError("forward maturity must follow trade date")
        side = self.side if isinstance(self.side, ForwardSide) else ForwardSide(self.side)
        counterparty = self.counterparty.strip() if self.counterparty else None
        object.__setattr__(self, "contract_id", contract_id)
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "notional", as_decimal(self.notional, "notional", positive=True))
        object.__setattr__(self, "strike", as_decimal(self.strike, "strike", positive=True))
        object.__setattr__(self, "counterparty", counterparty)

    @property
    def base_currency(self) -> str:
        return self.symbol[:3]

    @property
    def quote_currency(self) -> str:
        return self.symbol[3:]

    @property
    def quote_notional(self) -> Decimal:
        return self.notional * self.strike


@dataclass(frozen=True)
class ForwardQuote:
    contract_id: str
    fair_rate: Decimal
    contracted_rate: Decimal
    present_value: Decimal
    settlement_value: Decimal
    discount_factor: Decimal
    currency: str

    def as_dict(self) -> Mapping[str, object]:
        return {
            "contractId": self.contract_id,
            "fairRate": str(self.fair_rate),
            "contractedRate": str(self.contracted_rate),
            "presentValue": str(self.present_value),
            "settlementValue": str(self.settlement_value),
            "discountFactor": str(self.discount_factor),
            "currency": self.currency,
        }


def price_forward(contract: ForwardContract, curve: FxForwardCurve) -> ForwardQuote:
    if contract.symbol != curve.symbol:
        raise ValidationError("forward contract and curve symbols differ")
    if contract.maturity < curve.valuation_date:
        raise ValidationError("cannot value a matured forward")
    fair_rate = curve.forward(contract.maturity)
    settlement = contract.side.sign * contract.notional * (fair_rate - contract.strike)
    discount = curve.domestic.discount(contract.maturity)
    present_value = settlement * discount
    return ForwardQuote(
        contract.contract_id,
        fair_rate,
        contract.strike,
        present_value,
        settlement,
        discount,
        contract.quote_currency,
    )


def forward_cashflows(contract: ForwardContract) -> Mapping[str, Decimal]:
    """Return signed maturity cashflows from the holder's perspective."""

    base = contract.side.sign * contract.notional
    quote = -contract.side.sign * contract.quote_notional
    return {contract.base_currency: base, contract.quote_currency: quote}


def break_even_spot(
    contract: ForwardContract,
    curve: FxForwardCurve,
) -> Decimal:
    """Spot rate today that would make the forward's value exactly zero."""

    ratio = curve.foreign.discount(contract.maturity) / curve.domestic.discount(contract.maturity)
    return contract.strike / ratio


def forward_delta(contract: ForwardContract, curve: FxForwardCurve) -> Decimal:
    """Analytic spot delta expressed in quote currency per spot unit."""

    foreign_discount = curve.foreign.discount(contract.maturity)
    return contract.side.sign * contract.notional * foreign_discount


def forward_dv01(
    contract: ForwardContract,
    curve: FxForwardCurve,
    *,
    domestic: bool,
) -> Decimal:
    """Finite-difference sensitivity to a one basis-point parallel curve move."""

    base_value = price_forward(contract, curve).present_value
    bumped_curve = FxForwardCurve(
        curve.symbol,
        curve.spot,
        curve.domestic.bump(Decimal("1")) if domestic else curve.domestic,
        curve.foreign if domestic else curve.foreign.bump(Decimal("1")),
    )
    return price_forward(contract, bumped_curve).present_value - base_value
