"""Tax-lot acquisition and configurable disposal selection."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from threading import RLock
from typing import Dict, Iterable, Optional, Tuple

from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError
from forex_monitor.tax.models import LotDisposal, LotMethod, TaxLot, number, text, utc


class TaxLotLedger:
    def __init__(self) -> None:
        self._lots: Dict[str, TaxLot] = {}
        self._source_lots: Dict[Tuple[str, str], str] = {}
        self._disposals: Dict[str, Tuple[LotDisposal, ...]] = {}
        self._lock = RLock()

    def acquire(self, lot: TaxLot) -> TaxLot:
        if not isinstance(lot, TaxLot):
            raise ValidationError("lot must be TaxLot")
        source_key = (lot.account_id, lot.source_reference)
        with self._lock:
            existing = self._lots.get(lot.lot_id)
            if existing is not None:
                if existing == lot:
                    return existing
                raise StorageConflictError("tax lot id already exists")
            source_lot = self._source_lots.get(source_key)
            if source_lot is not None:
                existing = self._lots[source_lot]
                if existing == lot:
                    return existing
                raise StorageConflictError("source reference already created a tax lot")
            self._lots[lot.lot_id] = lot
            self._source_lots[source_key] = lot.lot_id
            return lot

    def dispose(
        self,
        *,
        disposal_id: str,
        account_id: str,
        instrument_id: str,
        quantity: object,
        unit_price: object,
        disposed_at: datetime,
        source_reference: str,
        method: LotMethod = LotMethod.FIFO,
        lot_ids: Iterable[str] = (),
        fees: object = "0",
    ) -> Tuple[LotDisposal, ...]:
        requested = number(quantity, "disposal quantity", positive=True)
        price = number(unit_price, "disposal price", non_negative=True)
        instant = utc(disposed_at, "disposed at")
        fee = number(fees, "disposal fees", non_negative=True)
        key = text(disposal_id, "disposal id")
        with self._lock:
            existing = self._disposals.get(key)
            if existing is not None:
                return existing
            eligible = [item for item in self._lots.values() if item.account_id == account_id and item.instrument_id == instrument_id and item.remaining_quantity > 0 and item.acquired_at <= instant]
            selected_ids = tuple(lot_ids)
            if method == LotMethod.SPECIFIC:
                if not selected_ids:
                    raise ValidationError("specific lot disposal requires lot ids")
                index = {item.lot_id: item for item in eligible}
                if any(item not in index for item in selected_ids):
                    raise ValidationError("specific lot is unavailable")
                eligible = [index[item] for item in selected_ids]
            elif selected_ids:
                raise ValidationError("lot ids are only valid for specific selection")
            elif method == LotMethod.FIFO:
                eligible.sort(key=lambda item: (item.acquired_at, item.lot_id))
            elif method == LotMethod.LIFO:
                eligible.sort(key=lambda item: (item.acquired_at, item.lot_id), reverse=True)
            elif method == LotMethod.HIFO:
                eligible.sort(key=lambda item: (-item.unit_cost, item.acquired_at, item.lot_id))
            if sum((item.remaining_quantity for item in eligible), Decimal("0")) < requested:
                raise ValidationError("insufficient tax lots for disposal")
            remaining = requested
            staged_lots = {}
            values = []
            fee_remaining = fee
            for index, lot in enumerate(eligible):
                if remaining == 0:
                    break
                allocated = min(remaining, lot.remaining_quantity)
                fraction = allocated / lot.original_quantity
                allocated_acquisition_fees = lot.fees * fraction
                disposal_fee = fee_remaining if index == len(eligible) - 1 or allocated == remaining else fee * allocated / requested
                fee_remaining -= disposal_fee
                proceeds = allocated * price - disposal_fee
                basis = allocated * lot.unit_cost + allocated_acquisition_fees
                values.append(LotDisposal("%s:%03d" % (key, len(values) + 1), lot.lot_id, account_id, instrument_id, allocated, proceeds, basis, instant, lot.acquired_at, source_reference))
                staged_lots[lot.lot_id] = replace(lot, remaining_quantity=lot.remaining_quantity - allocated)
                remaining -= allocated
            for lot_id, changed in staged_lots.items():
                self._lots[lot_id] = changed
            result = tuple(values)
            self._disposals[key] = result
            return result

    def open_lots(self, account_id: str, instrument_id: Optional[str] = None) -> Tuple[TaxLot, ...]:
        with self._lock:
            values = [item for item in self._lots.values() if item.account_id == account_id and item.remaining_quantity > 0 and (instrument_id is None or item.instrument_id == instrument_id)]
            return tuple(sorted(values, key=lambda item: (item.acquired_at, item.lot_id)))

    def disposals(self, *, account_id: Optional[str] = None, year: Optional[int] = None) -> Tuple[LotDisposal, ...]:
        with self._lock:
            values = [item for group in self._disposals.values() for item in group if (account_id is None or item.account_id == account_id) and (year is None or item.disposed_at.year == year)]
            return tuple(sorted(values, key=lambda item: (item.disposed_at, item.disposal_id)))

    def unrealized(self, account_id: str, marks: Dict[str, object]) -> Decimal:
        total = Decimal("0")
        with self._lock:
            for lot in self.open_lots(account_id):
                if lot.instrument_id not in marks:
                    raise ValidationError("missing tax-lot mark")
                mark = number(marks[lot.instrument_id], "mark", non_negative=True)
                total += lot.remaining_quantity * mark - lot.remaining_cost_basis
        return total
