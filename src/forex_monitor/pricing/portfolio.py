"""Heterogeneous pricing books and currency-normalized valuation reports."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, Iterable, Mapping, Optional, Sequence, Tuple, Union

from forex_monitor.errors import ValidationError
from forex_monitor.models import as_decimal
from forex_monitor.pricing.curves import FxForwardCurve
from forex_monitor.pricing.forwards import ForwardContract, price_forward
from forex_monitor.pricing.ndf import NdfContract, indicative_settlement
from forex_monitor.pricing.options import FxOption, price_option
from forex_monitor.pricing.swaps import FxSwap, price_swap


PricedInstrument = Union[ForwardContract, FxSwap, FxOption, NdfContract]


@dataclass(frozen=True)
class ValuationLine:
    instrument_id: str
    instrument_type: str
    currency: str
    local_value: Decimal
    reporting_value: Decimal
    market_reference: Decimal

    def as_dict(self) -> Mapping[str, object]:
        return {
            "instrumentId": self.instrument_id,
            "instrumentType": self.instrument_type,
            "currency": self.currency,
            "localValue": str(self.local_value),
            "reportingValue": str(self.reporting_value),
            "marketReference": str(self.market_reference),
        }


@dataclass(frozen=True)
class ValuationReport:
    reporting_currency: str
    lines: Tuple[ValuationLine, ...]

    @property
    def total(self) -> Decimal:
        return sum((line.reporting_value for line in self.lines), Decimal("0"))

    @property
    def by_type(self) -> Mapping[str, Decimal]:
        result: Dict[str, Decimal] = {}
        for line in self.lines:
            result[line.instrument_type] = result.get(line.instrument_type, Decimal("0")) + line.reporting_value
        return result

    @property
    def by_currency(self) -> Mapping[str, Decimal]:
        result: Dict[str, Decimal] = {}
        for line in self.lines:
            result[line.currency] = result.get(line.currency, Decimal("0")) + line.local_value
        return result

    def as_dict(self) -> Mapping[str, object]:
        return {
            "reportingCurrency": self.reporting_currency,
            "total": str(self.total),
            "lines": tuple(line.as_dict() for line in self.lines),
            "byType": {key: str(value) for key, value in self.by_type.items()},
            "byCurrency": {key: str(value) for key, value in self.by_currency.items()},
        }


class PricingBook:
    def __init__(self, reporting_currency: str = "USD") -> None:
        currency = reporting_currency.strip().upper()
        if len(currency) != 3 or not currency.isalpha():
            raise ValidationError("reporting currency must contain three letters")
        self.reporting_currency = currency
        self._instruments: Dict[str, PricedInstrument] = {}

    def add(self, instrument: PricedInstrument) -> None:
        identifier = instrument_id(instrument)
        if identifier in self._instruments:
            raise ValidationError("instrument id is already present in the pricing book")
        self._instruments[identifier] = instrument

    def remove(self, identifier: str) -> PricedInstrument:
        try:
            return self._instruments.pop(identifier)
        except KeyError as exc:
            raise ValidationError("instrument id is not present in the pricing book") from exc

    def instruments(self) -> Tuple[PricedInstrument, ...]:
        return tuple(self._instruments[key] for key in sorted(self._instruments))

    def value(
        self,
        curves: Mapping[str, FxForwardCurve],
        *,
        volatilities: Optional[Mapping[str, object]] = None,
        ndf_rates: Optional[Mapping[str, object]] = None,
        conversion_rates: Optional[Mapping[str, object]] = None,
    ) -> ValuationReport:
        lines = []
        for instrument in self.instruments():
            symbol = instrument.symbol
            try:
                curve = curves[symbol]
            except KeyError as exc:
                raise ValidationError("pricing curve is missing for " + symbol) from exc
            if isinstance(instrument, ForwardContract):
                quote = price_forward(instrument, curve)
                local = quote.present_value
                currency = quote.currency
                reference = quote.fair_rate
                kind = "forward"
            elif isinstance(instrument, FxSwap):
                quote = price_swap(instrument, curve)
                local = quote.present_value
                currency = quote.currency
                reference = quote.far_fair_rate
                kind = "swap"
            elif isinstance(instrument, FxOption):
                if volatilities is None or instrument.option_id not in volatilities:
                    raise ValidationError("option volatility is missing for " + instrument.option_id)
                quote = price_option(instrument, curve, volatilities[instrument.option_id])
                local = quote.value
                currency = quote.currency
                reference = quote.volatility
                kind = "option"
            else:
                if ndf_rates is None or instrument.contract_id not in ndf_rates:
                    raise ValidationError("NDF market rate is missing for " + instrument.contract_id)
                reference = as_decimal(ndf_rates[instrument.contract_id], "NDF market rate", positive=True)
                local = indicative_settlement(instrument, reference)
                currency = instrument.settlement_currency
                kind = "ndf"
            reporting = convert_value(local, currency, self.reporting_currency, conversion_rates or {})
            lines.append(
                ValuationLine(
                    instrument_id(instrument),
                    kind,
                    currency,
                    local,
                    reporting,
                    reference,
                )
            )
        return ValuationReport(self.reporting_currency, tuple(lines))


def instrument_id(instrument: PricedInstrument) -> str:
    if isinstance(instrument, ForwardContract):
        return instrument.contract_id
    if isinstance(instrument, FxSwap):
        return instrument.swap_id
    if isinstance(instrument, FxOption):
        return instrument.option_id
    return instrument.contract_id


def convert_value(
    value: Decimal,
    source: str,
    target: str,
    rates: Mapping[str, object],
) -> Decimal:
    if source == target:
        return value
    direct = source + target
    inverse = target + source
    if direct in rates:
        return value * as_decimal(rates[direct], "conversion rate", positive=True)
    if inverse in rates:
        return value / as_decimal(rates[inverse], "conversion rate", positive=True)
    raise ValidationError("conversion rate is missing for " + source + "/" + target)
