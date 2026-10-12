"""Cross-record validation and settlement convention resolution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Optional, Tuple

from forex_monitor.errors import ValidationError
from forex_monitor.reference_data.calendars import BusinessCalendarService
from forex_monitor.reference_data.identifiers import IdentifierRegistry
from forex_monitor.reference_data.instruments import InstrumentRegistry
from forex_monitor.reference_data.models import DayRoll, FxInstrument, IdentifierAssignment, IdentifierType, InstrumentType, decimal_value


@dataclass(frozen=True)
class SettlementTerms:
    instrument_id: str
    trade_date: date
    spot_date: date
    value_date: date
    calendars: Tuple[str, ...]
    fixing_date: Optional[date] = None
    settlement_currency: Optional[str] = None


@dataclass(frozen=True)
class NormalizedOrder:
    instrument_id: str
    symbol: str
    quantity: Decimal
    limit_price: Optional[Decimal]
    trade_date: date
    value_date: date


class ReferenceDataService:
    def __init__(self, instruments: InstrumentRegistry, calendars: BusinessCalendarService, identifiers: Optional[IdentifierRegistry] = None) -> None:
        self.instruments = instruments
        self.calendars = calendars
        self.identifiers = identifiers or IdentifierRegistry()

    def settlement_terms(
        self,
        instrument_id: str,
        *,
        trade_date: date,
        tenor_days: Optional[int] = None,
        requested_value_date: Optional[date] = None,
        fixing_lag: int = 2,
    ) -> SettlementTerms:
        instrument = self.instruments.instrument(instrument_id, on=trade_date)
        if not instrument.active:
            raise ValidationError("instrument is inactive")
        spot = self.calendars.add_business_days(trade_date, instrument.spot_lag, instrument.calendars)
        if requested_value_date is not None and tenor_days is not None:
            raise ValidationError("supply a tenor or requested value date, not both")
        if requested_value_date is not None:
            value_date = self.calendars.adjust(requested_value_date, instrument.calendars, instrument.day_roll)
        elif tenor_days is not None:
            if tenor_days < 0:
                raise ValidationError("tenor days cannot be negative")
            unadjusted = spot + date.resolution * tenor_days
            value_date = self.calendars.adjust(unadjusted, instrument.calendars, instrument.day_roll)
        else:
            value_date = spot
        fixing = None
        if instrument.instrument_type == InstrumentType.NDF:
            if fixing_lag < 0:
                raise ValidationError("fixing lag cannot be negative")
            fixing = self.calendars.add_business_days(value_date, -fixing_lag, instrument.calendars)
            if fixing <= trade_date:
                raise ValidationError("NDF fixing date must follow trade date")
        return SettlementTerms(instrument.instrument_id, trade_date, spot, value_date, instrument.calendars, fixing, instrument.settlement_currency)

    def normalize_order(
        self,
        instrument_id: str,
        *,
        quantity: object,
        limit_price: Optional[object],
        trade_date: date,
        value_date: Optional[date] = None,
    ) -> NormalizedOrder:
        instrument = self.instruments.instrument(instrument_id, on=trade_date)
        size = decimal_value(quantity, "quantity", positive=True)
        if not instrument.valid_quantity(size):
            raise ValidationError("order quantity does not meet instrument increment")
        price = None
        if limit_price is not None:
            raw = decimal_value(limit_price, "limit price", positive=True)
            quantum = Decimal("1").scaleb(-instrument.price_precision)
            price = raw.quantize(quantum, rounding=ROUND_HALF_EVEN)
        terms = self.settlement_terms(instrument_id, trade_date=trade_date, requested_value_date=value_date) if value_date else self.settlement_terms(instrument_id, trade_date=trade_date)
        return NormalizedOrder(instrument.instrument_id, instrument.symbol, size, price, trade_date, terms.value_date)

    def resolve_instrument(self, identifier_type: IdentifierType, value: str, *, on: date) -> FxInstrument:
        assignment = self.identifiers.resolve(identifier_type, value, on=on)
        if assignment.entity_type != "instrument":
            raise ValidationError("identifier does not refer to an instrument")
        return self.instruments.instrument(assignment.entity_id, on=on)

    def assign_identifier(self, assignment: IdentifierAssignment) -> IdentifierAssignment:
        if assignment.entity_type == "instrument":
            self.instruments.instrument(assignment.entity_id, on=assignment.valid_from)
        return self.identifiers.assign(assignment)
