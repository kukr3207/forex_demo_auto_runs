from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from forex_monitor.errors import StorageConflictError, ValidationError
from forex_monitor.regulatory.models import AssetClass, Party, ReportStatus, TradeEconomics
from forex_monitor.regulatory.reconciliation import RegulatoryReconciler, RepositoryRecord
from forex_monitor.regulatory.reporting import TransactionReportingService
from forex_monitor.regulatory.rules import ReportingRulebook


NOW = datetime(2026, 10, 12, 8, tzinfo=timezone.utc)
LEI_A = "5493001KJTIIGC8Y1R12"
LEI_B = "213800D1EI4B9WTWWD28"


def parties():
    return Party("bank", "Example Bank", LEI_A, "DE", True), Party("fund", "Example Fund", LEI_B, "GB", True)


def economics(asset_class=AssetClass.FORWARD):
    return TradeEconomics("trade-1", asset_class, "EUR", "USD", Decimal("1000000"), Decimal("1.10"), NOW, date(2026, 10, 14), maturity_date=date(2026, 11, 14))


def service():
    return TransactionReportingService(ReportingRulebook.standard())


def create_report(reporting_service=None):
    reporting_service = reporting_service or service()
    first, second = parties()
    report = reporting_service.create(report_id="report-1", regime="EMIR", economics=economics(), reporting_party=first, counterparty=second, unique_transaction_id="UTI-1", generated_at=NOW)
    return reporting_service, report


def test_rulebook_selects_regimes_by_country_and_asset_class() -> None:
    rules = ReportingRulebook.standard()
    assert rules.applicable_regimes(economics(), "DE") == ("EMIR", "MIFIR")
    assert rules.applicable_regimes(economics(), "US") == ("CFTC",)


def test_report_validates_submits_and_acknowledges() -> None:
    reporting, report = create_report()
    validated, findings = reporting.validate(report.report_id)
    assert not findings
    assert validated.status == ReportStatus.VALIDATED
    receipt = reporting.submit(report.report_id, submission_id="submission-1", destination="dtcc", submitted_at=NOW, expected_version=2)
    assert len(receipt.payload_hash) == 64
    reporting.acknowledge(receipt.submission_id, accepted=True, response_code="ACK")
    assert reporting.report(report.report_id).status == ReportStatus.ACKNOWLEDGED


def test_submission_is_idempotent_and_version_protected() -> None:
    reporting, report = create_report()
    reporting.validate(report.report_id)
    first = reporting.submit(report.report_id, submission_id="submission-1", destination="DTCC", submitted_at=NOW, expected_version=2)
    second = reporting.submit(report.report_id, submission_id="submission-1", destination="DTCC", submitted_at=NOW, expected_version=999)
    assert second == first


def test_missing_lei_prevents_validation() -> None:
    reporting = service()
    reporting_party, _ = parties()
    counterparty = Party("retail", "Retail Client", None, "GB", False)
    report = reporting.create(report_id="report-1", regime="EMIR", economics=economics(), reporting_party=reporting_party, counterparty=counterparty, unique_transaction_id="UTI-1", generated_at=NOW)
    unchanged, findings = reporting.validate(report.report_id)
    assert unchanged.status == ReportStatus.DRAFT
    assert {item.code for item in findings} == {"LEI_REQUIRED"}


def test_acknowledged_report_can_be_corrected_and_cancelled() -> None:
    reporting, report = create_report()
    reporting.validate(report.report_id)
    receipt = reporting.submit(report.report_id, submission_id="submission-1", destination="DTCC", submitted_at=NOW, expected_version=2)
    reporting.acknowledge(receipt.submission_id, accepted=True, response_code="ACK")
    correction = reporting.correct(report.report_id, new_report_id="report-2", generated_at=NOW + timedelta(hours=1), fields={"desk": "LDN"})
    assert correction.prior_report_id == report.report_id
    cancellation = reporting.cancel(report.report_id, new_report_id="report-3", generated_at=NOW + timedelta(hours=2))
    assert cancellation.action.value == "cancel"
    assert len(reporting.reports_for_uti("EMIR", "UTI-1")) == 3


def test_duplicate_new_uti_is_rejected_without_replacing_original() -> None:
    reporting, report = create_report()
    first, second = parties()
    with pytest.raises(StorageConflictError):
        reporting.create(report_id="other", regime="EMIR", economics=economics(), reporting_party=first, counterparty=second, unique_transaction_id="UTI-1", generated_at=NOW)
    assert reporting.report(report.report_id) == report


def test_overdue_returns_unsubmitted_work() -> None:
    reporting, report = create_report()
    assert reporting.overdue(NOW + timedelta(days=2)) == (report,)


def test_swap_requires_both_second_leg_fields() -> None:
    with pytest.raises(ValidationError):
        TradeEconomics("swap", AssetClass.SWAP, "EUR", "USD", 1, 1, NOW, date(2026, 10, 14), second_quantity=1)


def test_reconciliation_reports_clean_match() -> None:
    _, report = create_report()
    external = RepositoryRecord("EMIR", "UTI-1", "trade-1", "EURUSD", Decimal("1000000"), Decimal("1.10"), "new", "accepted", "row-1")
    result = RegulatoryReconciler().reconcile((report,), (external,))
    assert result.clean
    assert result.matched_count == 1


def test_reconciliation_identifies_missing_and_mismatched_records() -> None:
    _, report = create_report()
    external = RepositoryRecord("EMIR", "UTI-1", "trade-1", "EURUSD", Decimal("999999"), Decimal("1.10"), "new", "accepted", "row-1")
    external_only = RepositoryRecord("EMIR", "UTI-2", "trade-2", "GBPUSD", Decimal("10"), Decimal("1.25"), "new", "accepted", "row-2")
    result = RegulatoryReconciler().reconcile((report,), (external, external_only))
    assert {item.category for item in result.breaks} == {"field_mismatch", "external_only"}
    assert result.breaks[0].field == "quantity"
