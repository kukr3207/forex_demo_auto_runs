"""Deterministic regulatory transaction report construction and validation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from hashlib import sha256
import json
from typing import Dict, Mapping, Optional, Sequence, Tuple

from forex_monitor.errors import StorageConflictError, ValidationError
from forex_monitor.execution.fills import Fill
from forex_monitor.execution.orders import Order
from forex_monitor.models import ensure_utc


class ReportState(str, Enum):
    DRAFT = "draft"
    VALIDATED = "validated"
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class RegulatoryReport:
    report_id: str
    regime: str
    transaction_id: str
    event_type: str
    occurred_at: datetime
    reporting_party: str
    counterparty: Optional[str]
    instrument: str
    quantity: Decimal
    price: Decimal
    currency: str
    payload: Mapping[str, object]
    state: ReportState = ReportState.DRAFT
    external_reference: Optional[str] = None

    def canonical_payload(self) -> Mapping[str, object]:
        return {
            "reportId": self.report_id,
            "regime": self.regime,
            "transactionId": self.transaction_id,
            "eventType": self.event_type,
            "occurredAt": ensure_utc(self.occurred_at).isoformat(),
            "reportingParty": self.reporting_party,
            "counterparty": self.counterparty,
            "instrument": self.instrument,
            "quantity": str(self.quantity),
            "price": str(self.price),
            "currency": self.currency,
            "details": dict(sorted(self.payload.items())),
        }

    @property
    def digest(self) -> str:
        data = json.dumps(self.canonical_payload(), sort_keys=True, separators=(",", ":"), default=str)
        return sha256(data.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ValidationIssue:
    field: str
    message: str
    severity: str = "error"


class RegulatoryReporter:
    def __init__(self, reporting_party: str) -> None:
        self.reporting_party = reporting_party.strip()
        if not self.reporting_party:
            raise ValidationError("reporting party cannot be blank")
        self._reports: Dict[str, RegulatoryReport] = {}

    def from_fill(
        self,
        report_id: str,
        regime: str,
        order: Order,
        fill: Fill,
        *,
        counterparty: Optional[str] = None,
        payload: Optional[Mapping[str, object]] = None,
    ) -> RegulatoryReport:
        if order.order_id != fill.order_id or order.symbol != fill.symbol:
            raise ValidationError("order and fill do not describe the same transaction")
        return RegulatoryReport(
            report_id.strip(), regime.strip().upper(), fill.external_id or fill.fill_id,
            "new", fill.executed_at, self.reporting_party, counterparty,
            fill.symbol, fill.quantity, fill.price, fill.symbol[3:], payload or {},
        )

    def validate(self, report: RegulatoryReport) -> Tuple[ValidationIssue, ...]:
        issues = []
        required = {
            "report_id": report.report_id,
            "regime": report.regime,
            "transaction_id": report.transaction_id,
            "event_type": report.event_type,
            "reporting_party": report.reporting_party,
            "instrument": report.instrument,
            "currency": report.currency,
        }
        for field, value in required.items():
            if not str(value).strip():
                issues.append(ValidationIssue(field, "field cannot be blank"))
        if report.quantity <= 0:
            issues.append(ValidationIssue("quantity", "quantity must be positive"))
        if report.price <= 0:
            issues.append(ValidationIssue("price", "price must be positive"))
        if report.event_type not in {"new", "modify", "cancel", "correct"}:
            issues.append(ValidationIssue("event_type", "event type is not supported"))
        if len(report.currency) != 3:
            issues.append(ValidationIssue("currency", "currency must contain three letters"))
        return tuple(issues)

    def store(self, report: RegulatoryReport) -> RegulatoryReport:
        issues = self.validate(report)
        if issues:
            raise ValidationError("regulatory report failed validation", details={"issues": [item.message for item in issues]})
        existing = self._reports.get(report.report_id)
        if existing is not None:
            if existing.digest == report.digest:
                return existing
            raise StorageConflictError("regulatory report id was reused with different data")
        self._reports[report.report_id] = report
        return report

    def pending(self, regime: Optional[str] = None) -> Tuple[RegulatoryReport, ...]:
        normalized = regime.strip().upper() if regime else None
        return tuple(sorted(
            (
                report for report in self._reports.values()
                if report.state in {ReportState.DRAFT, ReportState.VALIDATED}
                and (normalized is None or report.regime == normalized)
            ),
            key=lambda item: (item.occurred_at, item.report_id),
        ))

    def export_jsonl(self, reports: Optional[Sequence[RegulatoryReport]] = None) -> str:
        values = reports if reports is not None else tuple(self._reports.values())
        return "\n".join(
            json.dumps(report.canonical_payload(), sort_keys=True, separators=(",", ":"), default=str)
            for report in sorted(values, key=lambda item: (item.occurred_at, item.report_id))
        )
