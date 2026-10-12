from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from forex_monitor.errors import StorageConflictError, ValidationError
from forex_monitor.tax.lots import TaxLotLedger
from forex_monitor.tax.models import IncomeType, LotMethod, TaxLot, TaxProfile, WithholdingRule
from forex_monitor.tax.reporting import TaxReportingService
from forex_monitor.tax.withholding import WithholdingEngine


NOW = datetime(2026, 10, 12, 8, tzinfo=timezone.utc)


def lot(lot_id, acquired, quantity, cost):
    return TaxLot(lot_id, "account", "EURUSD", "USD", acquired, Decimal(quantity), Decimal(quantity), Decimal(cost), source_reference=lot_id)


def test_fifo_disposal_allocates_lots_and_realized_gain() -> None:
    ledger = TaxLotLedger()
    ledger.acquire(lot("old", NOW - timedelta(days=400), "100", "1.0"))
    ledger.acquire(lot("new", NOW - timedelta(days=10), "100", "1.2"))
    disposal = ledger.dispose(disposal_id="sale", account_id="account", instrument_id="EURUSD", quantity="150", unit_price="1.5", disposed_at=NOW, source_reference="trade", method=LotMethod.FIFO)
    assert [item.lot_id for item in disposal] == ["old", "new"]
    assert sum((item.gain for item in disposal), Decimal("0")) == Decimal("65.0")
    assert disposal[0].long_term
    assert ledger.open_lots("account")[0].remaining_quantity == Decimal("50")


def test_hifo_disposal_uses_highest_cost_first() -> None:
    ledger = TaxLotLedger()
    ledger.acquire(lot("low", NOW, "10", "1"))
    ledger.acquire(lot("high", NOW, "10", "2"))
    disposal = ledger.dispose(disposal_id="sale", account_id="account", instrument_id="EURUSD", quantity="5", unit_price="3", disposed_at=NOW, source_reference="trade", method=LotMethod.HIFO)
    assert disposal[0].lot_id == "high"


def test_disposal_is_atomic_when_quantity_is_insufficient() -> None:
    ledger = TaxLotLedger()
    original = ledger.acquire(lot("one", NOW, "10", "1"))
    with pytest.raises(ValidationError):
        ledger.dispose(disposal_id="sale", account_id="account", instrument_id="EURUSD", quantity="11", unit_price="2", disposed_at=NOW, source_reference="trade")
    assert ledger.open_lots("account") == (original,)


def test_withholding_uses_treaty_and_backup_rates() -> None:
    engine = WithholdingEngine(backup_rate="0.24")
    engine.add_rule(WithholdingRule("rule", "US", IncomeType.INTEREST, Decimal("0.30"), treaty_rates=(("GB", Decimal("0.15")),)))
    engine.add_profile(TaxProfile("profile", "client", "GB", "TIN", "corporation", treaty_country="GB", certified_at=NOW - timedelta(days=1)))
    result = engine.calculate(calculation_id="calc", client_id="client", source_country="US", income_type=IncomeType.INTEREST, gross_amount="100", currency_code="USD", calculated_at=NOW)
    assert result.rate == Decimal("0.15")
    assert result.net_amount == Decimal("85.00")
    engine.add_profile(TaxProfile("uncertified", "other", "GB", "TIN2", "individual"))
    backup = engine.calculate(calculation_id="backup", client_id="other", source_country="US", income_type=IncomeType.INTEREST, gross_amount="100", currency_code="USD", calculated_at=NOW)
    assert backup.rate == Decimal("0.24")


def test_overlapping_withholding_rules_are_rejected() -> None:
    engine = WithholdingEngine()
    engine.add_rule(WithholdingRule("one", "US", IncomeType.INTEREST, Decimal("0.3")))
    with pytest.raises(StorageConflictError):
        engine.add_rule(WithholdingRule("two", "US", IncomeType.INTEREST, Decimal("0.2")))


def test_tax_statement_splits_short_and_long_term_and_corrects() -> None:
    ledger = TaxLotLedger()
    ledger.acquire(lot("old", NOW - timedelta(days=400), "10", "1"))
    ledger.acquire(lot("new", NOW - timedelta(days=10), "10", "1"))
    disposals = ledger.dispose(disposal_id="sale", account_id="account", instrument_id="EURUSD", quantity="20", unit_price="2", disposed_at=NOW, source_reference="trade")
    reports = TaxReportingService()
    statement = reports.generate(statement_id="statement", client_id="client", tax_year=NOW.year, currency_code="USD", disposals=disposals, withholdings=(), generated_at=NOW)
    assert statement.long_term_gain == Decimal("10")
    assert statement.short_term_gain == Decimal("10")
    correction = reports.generate(statement_id="correction", client_id="client", tax_year=NOW.year, currency_code="USD", disposals=disposals, withholdings=(), generated_at=NOW, correction_of=statement.statement_id)
    assert correction.version == 2
    assert reports.latest("client", NOW.year) == correction
