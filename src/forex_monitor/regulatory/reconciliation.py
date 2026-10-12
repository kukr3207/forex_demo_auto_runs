"""Compare internal reports with trade-repository extracts."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Dict, Iterable, Mapping, Optional, Tuple

from forex_monitor.errors import ValidationError
from forex_monitor.regulatory.models import TransactionReport, required


@dataclass(frozen=True)
class RepositoryRecord:
    regime: str
    uti: str
    trade_id: str
    symbol: str
    quantity: Decimal
    price: Decimal
    action: str
    status: str
    source_reference: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "regime", required(self.regime, "regime").upper())
        object.__setattr__(self, "uti", required(self.uti, "UTI"))
        object.__setattr__(self, "trade_id", required(self.trade_id, "trade id"))
        symbol = required(self.symbol, "symbol").upper().replace("/", "")
        if len(symbol) != 6:
            raise ValidationError("repository symbol is invalid")
        object.__setattr__(self, "symbol", symbol)
        try:
            quantity = Decimal(str(self.quantity))
            price = Decimal(str(self.price))
        except (InvalidOperation, ValueError) as exc:
            raise ValidationError("repository economics are invalid") from exc
        if quantity <= 0 or price <= 0:
            raise ValidationError("repository economics must be positive")
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "action", required(self.action, "action").lower())
        object.__setattr__(self, "status", required(self.status, "status").lower())
        object.__setattr__(self, "source_reference", required(self.source_reference, "source reference"))


@dataclass(frozen=True)
class ReconciliationBreak:
    regime: str
    uti: str
    category: str
    field: str
    internal_value: Optional[str]
    external_value: Optional[str]
    source_reference: Optional[str]


@dataclass(frozen=True)
class ReconciliationSummary:
    internal_count: int
    external_count: int
    matched_count: int
    breaks: Tuple[ReconciliationBreak, ...]

    @property
    def clean(self) -> bool:
        return not self.breaks


class RegulatoryReconciler:
    def __init__(self, *, quantity_tolerance: object = "0", price_tolerance: object = "0") -> None:
        self.quantity_tolerance = Decimal(str(quantity_tolerance))
        self.price_tolerance = Decimal(str(price_tolerance))
        if self.quantity_tolerance < 0 or self.price_tolerance < 0:
            raise ValidationError("reconciliation tolerances cannot be negative")

    def reconcile(
        self,
        internal: Iterable[TransactionReport],
        external: Iterable[RepositoryRecord],
    ) -> ReconciliationSummary:
        internal_values = tuple(internal)
        external_values = tuple(external)
        internal_index = self._latest_internal(internal_values)
        external_index = self._unique_external(external_values)
        breaks = []
        matched = 0
        all_keys = sorted(set(internal_index) | set(external_index))
        for regime, uti in all_keys:
            report = internal_index.get((regime, uti))
            record = external_index.get((regime, uti))
            if report is None:
                breaks.append(ReconciliationBreak(regime, uti, "external_only", "uti", None, uti, record.source_reference if record else None))
                continue
            if record is None:
                breaks.append(ReconciliationBreak(regime, uti, "internal_only", "uti", uti, None, None))
                continue
            item_breaks = self._compare(report, record)
            if item_breaks:
                breaks.extend(item_breaks)
            else:
                matched += 1
        return ReconciliationSummary(len(internal_values), len(external_values), matched, tuple(breaks))

    def _compare(self, report: TransactionReport, record: RepositoryRecord) -> Tuple[ReconciliationBreak, ...]:
        values = (
            ("tradeId", report.economics.trade_id, record.trade_id, False),
            ("symbol", report.economics.symbol, record.symbol, False),
            ("quantity", report.economics.quantity, record.quantity, self.quantity_tolerance),
            ("price", report.economics.price, record.price, self.price_tolerance),
            ("action", report.action.value, record.action, False),
        )
        result = []
        for field, internal, external, tolerance in values:
            equal = abs(internal - external) <= tolerance if isinstance(tolerance, Decimal) else internal == external
            if not equal:
                result.append(ReconciliationBreak(report.regime, report.unique_transaction_id, "field_mismatch", field, str(internal), str(external), record.source_reference))
        return tuple(result)

    @staticmethod
    def _latest_internal(values: Iterable[TransactionReport]) -> Dict[Tuple[str, str], TransactionReport]:
        result = {}
        for value in values:
            key = (value.regime, value.unique_transaction_id)
            current = result.get(key)
            if current is None or (value.generated_at, value.version, value.report_id) > (current.generated_at, current.version, current.report_id):
                result[key] = value
        return result

    @staticmethod
    def _unique_external(values: Iterable[RepositoryRecord]) -> Dict[Tuple[str, str], RepositoryRecord]:
        result = {}
        for value in values:
            key = (value.regime, value.uti)
            if key in result:
                raise ValidationError("repository extract contains duplicate UTI")
            result[key] = value
        return result
