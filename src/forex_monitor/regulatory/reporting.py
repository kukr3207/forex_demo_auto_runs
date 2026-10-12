"""Atomic report construction, validation, submission, and correction."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime
from threading import RLock
from typing import Dict, Mapping, Optional, Tuple

from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError
from forex_monitor.regulatory.models import (
    Party,
    ReportAction,
    ReportStatus,
    SubmissionReceipt,
    TradeEconomics,
    TransactionReport,
    ValidationFinding,
    required,
    utc,
)
from forex_monitor.regulatory.rules import ReportingRulebook


class TransactionReportingService:
    def __init__(self, rulebook: ReportingRulebook) -> None:
        self.rulebook = rulebook
        self._reports: Dict[str, TransactionReport] = {}
        self._uti_reports: Dict[Tuple[str, str], list[str]] = {}
        self._trade_reports: Dict[Tuple[str, str], list[str]] = {}
        self._submissions: Dict[str, SubmissionReceipt] = {}
        self._report_submissions: Dict[str, list[str]] = {}
        self._lock = RLock()

    def create(
        self,
        *,
        report_id: str,
        regime: str,
        economics: TradeEconomics,
        reporting_party: Party,
        counterparty: Party,
        unique_transaction_id: str,
        generated_at: datetime,
        fields: Mapping[str, object] = {},
    ) -> TransactionReport:
        instant = utc(generated_at, "generated at")
        report = TransactionReport(
            report_id=report_id,
            regime=regime,
            action=ReportAction.NEW,
            status=ReportStatus.DRAFT,
            economics=economics,
            reporting_party=reporting_party,
            counterparty=counterparty,
            unique_transaction_id=unique_transaction_id,
            generated_at=instant,
            report_deadline=self.rulebook.deadline_for(regime, economics),
            fields=tuple((key, str(value)) for key, value in fields.items()),
        )
        with self._lock:
            self._insert(report)
            return report

    def validate(self, report_id: str) -> Tuple[TransactionReport, Tuple[ValidationFinding, ...]]:
        with self._lock:
            current = self._require(report_id)
            if current.status not in {ReportStatus.DRAFT, ReportStatus.REJECTED}:
                raise ValidationError("report cannot be validated from its current status")
            findings = self.rulebook.validate(current)
            errors = tuple(item for item in findings if item.severity == "error")
            if errors:
                return current, findings
            changed = replace(current, status=ReportStatus.VALIDATED, version=current.version + 1)
            self._reports[current.report_id] = changed
            return changed, findings

    def submit(
        self,
        report_id: str,
        *,
        submission_id: str,
        destination: str,
        submitted_at: datetime,
        expected_version: int,
    ) -> SubmissionReceipt:
        with self._lock:
            current = self._require(report_id)
            existing = self._submissions.get(submission_id)
            if existing is not None:
                if existing.report_id == current.report_id and existing.destination == destination.upper():
                    return existing
                raise StorageConflictError("submission id already exists")
            if current.version != expected_version:
                raise StorageConflictError("report version does not match")
            if current.status != ReportStatus.VALIDATED:
                raise ValidationError("only validated reports can be submitted")
            payload = json.dumps(current.as_dict(), sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
            receipt = SubmissionReceipt(
                submission_id,
                current.report_id,
                destination,
                submitted_at,
                hashlib.sha256(payload).hexdigest(),
            )
            self._submissions[receipt.submission_id] = receipt
            self._report_submissions[current.report_id].append(receipt.submission_id)
            self._reports[current.report_id] = replace(current, status=ReportStatus.SUBMITTED, version=current.version + 1)
            return receipt

    def acknowledge(
        self,
        submission_id: str,
        *,
        accepted: bool,
        response_code: str,
        response_message: str = "",
    ) -> SubmissionReceipt:
        key = required(submission_id, "submission id")
        with self._lock:
            current = self._submissions.get(key)
            if current is None:
                raise NotFoundError("submission does not exist")
            if current.accepted is not None:
                if current.accepted == accepted and current.response_code == response_code:
                    return current
                raise StorageConflictError("submission already has a different response")
            changed = replace(current, accepted=accepted, response_code=required(response_code, "response code"), response_message=response_message.strip() or None)
            self._submissions[key] = changed
            report = self._reports[current.report_id]
            self._reports[report.report_id] = replace(report, status=ReportStatus.ACKNOWLEDGED if accepted else ReportStatus.REJECTED, version=report.version + 1)
            return changed

    def correct(
        self,
        report_id: str,
        *,
        new_report_id: str,
        generated_at: datetime,
        economics: Optional[TradeEconomics] = None,
        fields: Optional[Mapping[str, object]] = None,
    ) -> TransactionReport:
        with self._lock:
            prior = self._require(report_id)
            if prior.status not in {ReportStatus.ACKNOWLEDGED, ReportStatus.REJECTED}:
                raise ValidationError("only acknowledged or rejected reports can be corrected")
            value = TransactionReport(
                report_id=new_report_id,
                regime=prior.regime,
                action=ReportAction.CORRECT,
                status=ReportStatus.DRAFT,
                economics=economics or prior.economics,
                reporting_party=prior.reporting_party,
                counterparty=prior.counterparty,
                unique_transaction_id=prior.unique_transaction_id,
                generated_at=generated_at,
                report_deadline=prior.report_deadline,
                prior_report_id=prior.report_id,
                fields=tuple((key, str(value)) for key, value in fields.items()) if fields is not None else prior.fields,
            )
            self._insert(value)
            return value

    def cancel(self, report_id: str, *, new_report_id: str, generated_at: datetime) -> TransactionReport:
        with self._lock:
            prior = self._require(report_id)
            if prior.status != ReportStatus.ACKNOWLEDGED:
                raise ValidationError("only acknowledged reports can be cancelled")
            value = replace(
                prior,
                report_id=new_report_id,
                action=ReportAction.CANCEL,
                status=ReportStatus.DRAFT,
                generated_at=utc(generated_at, "generated at"),
                prior_report_id=prior.report_id,
                version=1,
            )
            self._insert(value)
            return value

    def report(self, report_id: str) -> Optional[TransactionReport]:
        with self._lock:
            return self._reports.get(report_id.strip())

    def reports_for_trade(self, regime: str, trade_id: str) -> Tuple[TransactionReport, ...]:
        with self._lock:
            ids = self._trade_reports.get((regime.upper(), trade_id), ())
            return tuple(self._reports[item] for item in ids)

    def reports_for_uti(self, regime: str, uti: str) -> Tuple[TransactionReport, ...]:
        with self._lock:
            ids = self._uti_reports.get((regime.upper(), uti), ())
            return tuple(self._reports[item] for item in ids)

    def submissions(self, report_id: str) -> Tuple[SubmissionReceipt, ...]:
        with self._lock:
            return tuple(self._submissions[item] for item in self._report_submissions.get(report_id.strip(), ()))

    def overdue(self, at: datetime) -> Tuple[TransactionReport, ...]:
        instant = utc(at, "overdue time")
        with self._lock:
            values = [
                item for item in self._reports.values()
                if item.reportable
                and item.report_deadline is not None
                and item.report_deadline < instant
                and item.status in {ReportStatus.DRAFT, ReportStatus.VALIDATED, ReportStatus.REJECTED}
            ]
            return tuple(sorted(values, key=lambda item: (item.report_deadline, item.report_id)))

    def _insert(self, report: TransactionReport) -> None:
        existing = self._reports.get(report.report_id)
        if existing is not None:
            if existing == report:
                return
            raise StorageConflictError("report id already exists")
        chain = self._uti_reports.setdefault((report.regime, report.unique_transaction_id), [])
        if report.action == ReportAction.NEW and chain:
            raise StorageConflictError("regime already has a new report for this UTI")
        self._reports[report.report_id] = report
        chain.append(report.report_id)
        self._trade_reports.setdefault((report.regime, report.economics.trade_id), []).append(report.report_id)
        self._report_submissions[report.report_id] = []

    def _require(self, report_id: str) -> TransactionReport:
        value = self._reports.get(required(report_id, "report id"))
        if value is None:
            raise NotFoundError("transaction report does not exist")
        return value
