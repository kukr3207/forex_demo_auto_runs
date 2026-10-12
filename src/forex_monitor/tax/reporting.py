"""Annual tax statement aggregation and correction history."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from threading import RLock
from typing import Dict, Iterable, Optional, Tuple

from forex_monitor.errors import StorageConflictError, ValidationError
from forex_monitor.tax.models import LotDisposal, TaxStatement, WithholdingResult, currency, text, utc


class TaxReportingService:
    def __init__(self) -> None:
        self._statements: Dict[str, TaxStatement] = {}
        self._client_year: Dict[Tuple[str, int], list[str]] = {}
        self._lock = RLock()

    def generate(
        self,
        *,
        statement_id: str,
        client_id: str,
        tax_year: int,
        currency_code: str,
        disposals: Iterable[LotDisposal],
        withholdings: Iterable[WithholdingResult],
        generated_at: datetime,
        correction_of: Optional[str] = None,
    ) -> TaxStatement:
        if tax_year < 1900 or tax_year > 9999:
            raise ValidationError("tax year is invalid")
        disposal_values = tuple(disposals)
        withholding_values = tuple(withholdings)
        if any(item.disposed_at.year != tax_year for item in disposal_values):
            raise ValidationError("disposal is outside statement tax year")
        if any(item.calculated_at.year != tax_year or item.client_id != client_id for item in withholding_values):
            raise ValidationError("withholding is outside statement scope")
        short = sum((item.gain for item in disposal_values if not item.long_term), Decimal("0"))
        long = sum((item.gain for item in disposal_values if item.long_term), Decimal("0"))
        income = sum((item.gross_amount for item in withholding_values), Decimal("0"))
        withheld = sum((item.withheld_amount for item in withholding_values), Decimal("0"))
        version = 1
        with self._lock:
            if correction_of:
                prior = self._statements.get(correction_of)
                if prior is None or prior.client_id != client_id or prior.tax_year != tax_year:
                    raise ValidationError("corrected statement is invalid")
                version = prior.version + 1
            statement = TaxStatement(text(statement_id, "statement id"), text(client_id, "client id"), tax_year, currency(currency_code), short, long, income, withheld, utc(generated_at, "generated at"), len(disposal_values) + len(withholding_values), correction_of, version)
            existing = self._statements.get(statement.statement_id)
            if existing is not None:
                if existing == statement:
                    return existing
                raise StorageConflictError("tax statement id already exists")
            if correction_of is None and self._client_year.get((client_id, tax_year)):
                raise StorageConflictError("client already has an original statement for tax year")
            self._statements[statement.statement_id] = statement
            self._client_year.setdefault((client_id, tax_year), []).append(statement.statement_id)
            return statement

    def statements(self, client_id: str, tax_year: Optional[int] = None) -> Tuple[TaxStatement, ...]:
        with self._lock:
            values = [item for item in self._statements.values() if item.client_id == client_id and (tax_year is None or item.tax_year == tax_year)]
            return tuple(sorted(values, key=lambda item: (item.tax_year, item.version)))

    def latest(self, client_id: str, tax_year: int) -> Optional[TaxStatement]:
        values = self.statements(client_id, tax_year)
        return values[-1] if values else None
