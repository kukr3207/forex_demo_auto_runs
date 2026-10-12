"""Effective-dated currencies and FX instruments with safe amendments."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from threading import RLock
from typing import Dict, Iterable, Optional, Tuple

from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError
from forex_monitor.reference_data.models import CurrencyDefinition, FxInstrument, InstrumentType, currency_code, required


class InstrumentRegistry:
    def __init__(self) -> None:
        self._currencies: Dict[Tuple[str, date], CurrencyDefinition] = {}
        self._instruments: Dict[Tuple[str, date], FxInstrument] = {}
        self._symbol_index: Dict[str, list[Tuple[str, date]]] = {}
        self._lock = RLock()

    def add_currency(self, definition: CurrencyDefinition) -> CurrencyDefinition:
        if not isinstance(definition, CurrencyDefinition):
            raise ValidationError("definition must be CurrencyDefinition")
        key = (definition.code, definition.effective_from)
        with self._lock:
            existing = self._currencies.get(key)
            if existing is not None:
                if existing == definition:
                    return existing
                raise StorageConflictError("currency version already exists")
            self._check_currency_overlap(definition)
            self._currencies[key] = definition
            return definition

    def add_instrument(self, instrument: FxInstrument) -> FxInstrument:
        if not isinstance(instrument, FxInstrument):
            raise ValidationError("instrument must be FxInstrument")
        key = (instrument.instrument_id, instrument.effective_from)
        with self._lock:
            existing = self._instruments.get(key)
            if existing is not None:
                if existing == instrument:
                    return existing
                raise StorageConflictError("instrument version already exists")
            self.currency(instrument.base_currency, on=instrument.effective_from)
            self.currency(instrument.quote_currency, on=instrument.effective_from)
            if instrument.settlement_currency:
                self.currency(instrument.settlement_currency, on=instrument.effective_from)
            self._check_instrument_overlap(instrument)
            self._instruments[key] = instrument
            self._symbol_index.setdefault(instrument.symbol, []).append(key)
            return instrument

    def amend_instrument(self, instrument_id: str, *, effective_from: date, expected_version: int, **changes: object) -> FxInstrument:
        with self._lock:
            current = self.instrument(instrument_id, on=effective_from)
            if current.version != expected_version:
                raise StorageConflictError("instrument version does not match")
            if effective_from <= current.effective_from:
                raise ValidationError("amendment must start after current version")
            allowed = {"price_precision", "quantity_precision", "minimum_quantity", "quantity_increment", "spot_lag", "calendars", "day_roll", "fixing_source", "active"}
            unknown = set(changes) - allowed
            if unknown:
                raise ValidationError("unsupported instrument amendment: %s" % sorted(unknown)[0])
            closed = replace(current, effective_until=effective_from - date.resolution)
            replacement = replace(current, effective_from=effective_from, effective_until=None, version=current.version + 1, **changes)
            self._instruments[(closed.instrument_id, closed.effective_from)] = closed
            self._instruments[(replacement.instrument_id, replacement.effective_from)] = replacement
            self._symbol_index[replacement.symbol].append((replacement.instrument_id, replacement.effective_from))
            return replacement

    def currency(self, code: str, *, on: date) -> CurrencyDefinition:
        normalized = currency_code(code)
        with self._lock:
            matches = [item for (item_code, _), item in self._currencies.items() if item_code == normalized and item.effective_on(on)]
            if not matches:
                raise NotFoundError("currency is not defined on requested date")
            if len(matches) > 1:
                raise StorageConflictError("currency definitions overlap")
            return matches[0]

    def instrument(self, instrument_id: str, *, on: date) -> FxInstrument:
        key = required(instrument_id, "instrument id")
        with self._lock:
            matches = [item for (item_id, _), item in self._instruments.items() if item_id == key and item.effective_on(on)]
            if not matches:
                raise NotFoundError("instrument is not defined on requested date")
            if len(matches) > 1:
                raise StorageConflictError("instrument definitions overlap")
            return matches[0]

    def by_symbol(self, symbol: str, *, on: date, instrument_type: Optional[InstrumentType] = None) -> Tuple[FxInstrument, ...]:
        normalized = required(symbol, "symbol").upper().replace("/", "")
        with self._lock:
            values = [self._instruments[key] for key in self._symbol_index.get(normalized, ())]
            result = [item for item in values if item.effective_on(on) and (instrument_type is None or item.instrument_type == instrument_type)]
            return tuple(sorted(result, key=lambda item: (item.instrument_type.value, item.instrument_id)))

    def active_instruments(self, *, on: date) -> Tuple[FxInstrument, ...]:
        with self._lock:
            values = [item for item in self._instruments.values() if item.active and item.effective_on(on)]
            return tuple(sorted(values, key=lambda item: (item.symbol, item.instrument_type.value, item.instrument_id)))

    def _check_currency_overlap(self, candidate: CurrencyDefinition) -> None:
        for (code, _), item in self._currencies.items():
            if code == candidate.code and self._dates_overlap(item.effective_from, item.effective_until, candidate.effective_from, candidate.effective_until):
                raise StorageConflictError("currency definitions overlap")
            if item.numeric_code == candidate.numeric_code and item.code != candidate.code and self._dates_overlap(item.effective_from, item.effective_until, candidate.effective_from, candidate.effective_until):
                raise StorageConflictError("numeric currency code is already assigned")

    def _check_instrument_overlap(self, candidate: FxInstrument) -> None:
        for (instrument_id, _), item in self._instruments.items():
            if instrument_id == candidate.instrument_id and self._dates_overlap(item.effective_from, item.effective_until, candidate.effective_from, candidate.effective_until):
                raise StorageConflictError("instrument definitions overlap")

    @staticmethod
    def _dates_overlap(left_start: date, left_end: Optional[date], right_start: date, right_end: Optional[date]) -> bool:
        return (left_end is None or right_start <= left_end) and (right_end is None or left_start <= right_end)
