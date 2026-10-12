"""Non-deliverable forwards, fixing validation, and settlement."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Mapping, Optional

from forex_monitor.errors import ValidationError
from forex_monitor.models import as_decimal, ensure_utc, normalize_symbol


class NdfSettlementFormula(str, Enum):
    QUOTE_CURRENCY = "quote_currency"
    BASE_CURRENCY = "base_currency"


@dataclass(frozen=True)
class NdfContract:
    contract_id: str
    symbol: str
    notional: Decimal
    strike: Decimal
    fixing_date: date
    settlement_date: date
    settlement_currency: str
    formula: NdfSettlementFormula = NdfSettlementFormula.QUOTE_CURRENCY
    buy_base: bool = True

    def __post_init__(self) -> None:
        identifier = self.contract_id.strip()
        symbol = normalize_symbol(self.symbol)
        currency = self.settlement_currency.strip().upper()
        if not identifier:
            raise ValidationError("NDF contract id cannot be blank")
        if len(symbol) != 6:
            raise ValidationError("NDF symbol must be a currency pair")
        if self.settlement_date < self.fixing_date:
            raise ValidationError("NDF settlement cannot precede fixing")
        if currency not in {symbol[:3], symbol[3:]}:
            raise ValidationError("settlement currency must belong to the currency pair")
        formula = self.formula if isinstance(self.formula, NdfSettlementFormula) else NdfSettlementFormula(self.formula)
        object.__setattr__(self, "contract_id", identifier)
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "settlement_currency", currency)
        object.__setattr__(self, "formula", formula)
        object.__setattr__(self, "notional", as_decimal(self.notional, "notional", positive=True))
        object.__setattr__(self, "strike", as_decimal(self.strike, "strike", positive=True))


@dataclass(frozen=True)
class NdfFixing:
    symbol: str
    rate: Decimal
    observed_at: datetime
    source: str
    official: bool = True

    def __post_init__(self) -> None:
        source = self.source.strip()
        if not source:
            raise ValidationError("fixing source cannot be blank")
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(self, "rate", as_decimal(self.rate, "fixing rate", positive=True))
        object.__setattr__(self, "observed_at", ensure_utc(self.observed_at, "fixing time"))
        object.__setattr__(self, "source", source)


@dataclass(frozen=True)
class NdfSettlement:
    contract_id: str
    fixing_rate: Decimal
    amount: Decimal
    currency: str
    value_date: date
    source: str

    def as_dict(self) -> Mapping[str, object]:
        return {
            "contractId": self.contract_id,
            "fixingRate": str(self.fixing_rate),
            "amount": str(self.amount),
            "currency": self.currency,
            "valueDate": self.value_date.isoformat(),
            "source": self.source,
        }


def settle_ndf(contract: NdfContract, fixing: NdfFixing) -> NdfSettlement:
    if contract.symbol != fixing.symbol:
        raise ValidationError("NDF fixing symbol does not match the contract")
    if fixing.observed_at.date() != contract.fixing_date:
        raise ValidationError("NDF fixing was not observed on the fixing date")
    sign = Decimal("1") if contract.buy_base else Decimal("-1")
    if contract.formula is NdfSettlementFormula.QUOTE_CURRENCY:
        amount = sign * contract.notional * (fixing.rate - contract.strike)
        expected_currency = contract.symbol[3:]
    else:
        amount = sign * contract.notional * (fixing.rate - contract.strike) / fixing.rate
        expected_currency = contract.symbol[:3]
    if contract.settlement_currency != expected_currency:
        amount = convert_settlement(amount, expected_currency, contract.settlement_currency, fixing.rate, contract.symbol)
    return NdfSettlement(
        contract.contract_id,
        fixing.rate,
        amount,
        contract.settlement_currency,
        contract.settlement_date,
        fixing.source,
    )


def convert_settlement(
    amount: Decimal,
    source_currency: str,
    target_currency: str,
    rate: Decimal,
    symbol: str,
) -> Decimal:
    if source_currency == target_currency:
        return amount
    base, quote = symbol[:3], symbol[3:]
    if source_currency == quote and target_currency == base:
        return amount / rate
    if source_currency == base and target_currency == quote:
        return amount * rate
    raise ValidationError("cannot convert NDF settlement outside its currency pair")


def indicative_settlement(contract: NdfContract, market_rate: object) -> Decimal:
    rate = as_decimal(market_rate, "market rate", positive=True)
    sign = Decimal("1") if contract.buy_base else Decimal("-1")
    if contract.formula is NdfSettlementFormula.BASE_CURRENCY:
        return sign * contract.notional * (rate - contract.strike) / rate
    return sign * contract.notional * (rate - contract.strike)
