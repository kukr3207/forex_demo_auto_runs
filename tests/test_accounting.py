from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from forex_monitor.accounting.billing import BillingAccounts, BillingService
from forex_monitor.accounting.fees import FeeEngine, FeeSchedule, FeeTier
from forex_monitor.accounting.ledger import GeneralLedger
from forex_monitor.accounting.models import Account, AccountType, FeeCharge, InvoiceStatus, JournalLine, Payment
from forex_monitor.accounting.reconciliation import BankTransaction, LedgerReconciler
from forex_monitor.errors import StorageConflictError, ValidationError


NOW = datetime(2026, 10, 12, 8, tzinfo=timezone.utc)
TODAY = NOW.date()


def ledger():
    value = GeneralLedger()
    for account in (
        Account("cash", "1000", "Cash", AccountType.ASSET, "USD"),
        Account("receivable", "1100", "Receivables", AccountType.ASSET, "USD"),
        Account("revenue", "4000", "Fee revenue", AccountType.REVENUE, "USD"),
        Account("tax", "2100", "Tax payable", AccountType.LIABILITY, "USD"),
        Account("expense", "5000", "Expense", AccountType.EXPENSE, "USD"),
    ):
        value.add_account(account)
    return value


def charge(charge_id="c1", amount="100"):
    return FeeCharge(charge_id, "schedule", "client", "execution", "USD", Decimal("1"), Decimal("0"), Decimal(amount), NOW, charge_id)


def test_balanced_entry_posts_and_updates_account_balances() -> None:
    book = ledger()
    entry = book.create_entry(entry_id="entry", journal="GENERAL", accounting_date=TODAY, currency="USD", description="Capital", lines=(JournalLine("d", "cash", debit=100), JournalLine("c", "revenue", credit=100)), created_at=NOW)
    posted = book.post(entry.entry_id, posted_at=NOW, expected_version=1)
    assert posted.status.value == "posted"
    assert book.account_balance("cash", as_of=TODAY).balance == Decimal("100")
    assert book.account_balance("revenue", as_of=TODAY).balance == Decimal("100")
    assert book.trial_balance(as_of=TODAY, currency="USD").balanced


def test_unbalanced_entry_is_rejected_before_storage() -> None:
    book = ledger()
    with pytest.raises(ValidationError):
        book.create_entry(entry_id="bad", journal="GENERAL", accounting_date=TODAY, currency="USD", description="Bad", lines=(JournalLine("d", "cash", debit=100), JournalLine("c", "revenue", credit=99)), created_at=NOW)
    assert book.entry("bad") is None


def test_reversal_is_balanced_and_idempotent() -> None:
    book = ledger()
    entry = book.create_entry(entry_id="entry", journal="GENERAL", accounting_date=TODAY, currency="USD", description="Capital", lines=(JournalLine("d", "cash", debit=100), JournalLine("c", "revenue", credit=100)), created_at=NOW)
    book.post(entry.entry_id, posted_at=NOW, expected_version=1)
    reversal = book.reverse(entry.entry_id, reversal_id="reverse", accounting_date=TODAY, created_at=NOW, posted_at=NOW)
    assert book.reverse(entry.entry_id, reversal_id="reverse", accounting_date=TODAY, created_at=NOW, posted_at=NOW) == reversal
    assert book.account_balance("cash", as_of=TODAY).balance == 0


def test_fee_engine_uses_volume_tier_caps_tax_and_replay() -> None:
    engine = FeeEngine()
    engine.add_schedule(FeeSchedule("schedule", "client", "execution", "USD", "notional", (FeeTier(0, Decimal("0.001")), FeeTier(100000, Decimal("0.0005"))), minimum_charge=Decimal("5"), maximum_charge=Decimal("100"), tax_rate=Decimal("0.1")))
    result = engine.calculate(charge_id="charge", client_id="client", charge_type="execution", quantity=1, notional=200000, occurred_at=NOW, source_reference="trade")
    assert result.charge.amount == Decimal("100.00")
    assert result.tax_amount == Decimal("10.00")
    assert engine.calculate(charge_id="other", client_id="client", charge_type="execution", quantity=999, notional=1, occurred_at=NOW, source_reference="trade") == result


def test_overlapping_fee_schedules_are_rejected() -> None:
    engine = FeeEngine()
    first = FeeSchedule("one", "client", "execution", "USD", "flat", (FeeTier(0, 0, 10),), effective_from=NOW)
    engine.add_schedule(first)
    with pytest.raises(StorageConflictError):
        engine.add_schedule(FeeSchedule("two", "client", "execution", "USD", "flat", (FeeTier(0, 0, 5),), effective_from=NOW + timedelta(days=1)))


def test_invoice_issue_payment_and_ledger_posting() -> None:
    book = ledger()
    billing = BillingService(book)
    billing.configure_accounts("USD", BillingAccounts("receivable", "revenue", "tax", "cash"))
    invoice = billing.create_from_charges(invoice_id="invoice", client_id="client", currency="USD", issue_date=TODAY, due_days=30, charges=(charge(),), tax_rates={"execution": "0.10"})
    issued = billing.issue(invoice.invoice_id, issued_at=NOW, expected_version=1)
    assert issued.total == Decimal("110.00")
    partial = billing.record_payment(Payment("p1", invoice.invoice_id, Decimal("50"), "USD", NOW, "bank-1"))
    assert partial.status == InvoiceStatus.PARTIALLY_PAID
    paid = billing.record_payment(Payment("p2", invoice.invoice_id, Decimal("60"), "USD", NOW, "bank-2"))
    assert paid.status == InvoiceStatus.PAID
    assert book.account_balance("receivable", as_of=TODAY).balance == 0


def test_invoice_rejects_duplicate_charge_assignment() -> None:
    billing = BillingService()
    billing.create_from_charges(invoice_id="one", client_id="client", currency="USD", issue_date=TODAY, due_days=1, charges=(charge(),))
    with pytest.raises(StorageConflictError):
        billing.create_from_charges(invoice_id="two", client_id="client", currency="USD", issue_date=TODAY, due_days=1, charges=(charge(),))


def test_overdue_and_aging_bucket_outstanding_amounts() -> None:
    billing = BillingService()
    invoice = billing.create_from_charges(invoice_id="invoice", client_id="client", currency="USD", issue_date=TODAY - timedelta(days=45), due_days=10, charges=(charge(),))
    billing.issue(invoice.invoice_id, issued_at=NOW - timedelta(days=45), expected_version=1)
    changed = billing.mark_overdue(TODAY)
    assert changed[0].status == InvoiceStatus.OVERDUE
    aging = billing.aging(TODAY)[0]
    assert aging.days_31_60 == Decimal("100")


def test_bank_reconciliation_matches_reference_and_amount() -> None:
    book = ledger()
    entry = book.create_entry(entry_id="payment:p1", journal="CASH", accounting_date=TODAY, currency="USD", description="Payment invoice", lines=(JournalLine("d", "cash", debit=100), JournalLine("c", "receivable", credit=100)), created_at=NOW, source_reference="bank-1")
    book.post(entry.entry_id, posted_at=NOW, expected_version=1)
    result = LedgerReconciler().reconcile_bank((BankTransaction("bank", NOW, 100, "USD", "bank-1"),), book, journal="CASH", from_date=TODAY, to_date=TODAY)
    assert result.matches[0].ledger_entry_id == entry.entry_id
    assert not result.unmatched_bank_ids


def test_receivables_control_reconciles_to_open_invoices() -> None:
    book = ledger()
    billing = BillingService(book)
    billing.configure_accounts("USD", BillingAccounts("receivable", "revenue", "tax", "cash"))
    invoice = billing.create_from_charges(invoice_id="invoice", client_id="client", currency="USD", issue_date=TODAY, due_days=30, charges=(charge(),))
    billing.issue(invoice.invoice_id, issued_at=NOW, expected_version=1)
    result = LedgerReconciler.reconcile_receivables(billing.invoices(), book, receivable_account_id="receivable", as_of=TODAY)
    assert result.difference == 0
    assert result.subledger_balance == Decimal("100")
