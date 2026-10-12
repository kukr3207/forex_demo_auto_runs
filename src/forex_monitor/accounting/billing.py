"""Invoice generation, issue controls, payments, aging, and ledger posting."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from threading import RLock
from typing import Dict, Iterable, Mapping, Optional, Tuple

from forex_monitor.accounting.ledger import GeneralLedger
from forex_monitor.accounting.models import (
    FeeCharge,
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    JournalLine,
    Payment,
    text,
    utc,
)
from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError


@dataclass(frozen=True)
class BillingAccounts:
    receivable_account_id: str
    revenue_account_id: str
    tax_account_id: Optional[str] = None
    cash_account_id: Optional[str] = None


@dataclass(frozen=True)
class AgingBucket:
    client_id: str
    currency: str
    current: Decimal
    days_1_30: Decimal
    days_31_60: Decimal
    days_61_90: Decimal
    over_90: Decimal

    @property
    def total(self) -> Decimal:
        return self.current + self.days_1_30 + self.days_31_60 + self.days_61_90 + self.over_90


class BillingService:
    def __init__(self, ledger: Optional[GeneralLedger] = None) -> None:
        self.ledger = ledger
        self._invoices: Dict[str, Invoice] = {}
        self._payments: Dict[str, Payment] = {}
        self._invoice_payments: Dict[str, list[str]] = {}
        self._charge_invoice: Dict[str, str] = {}
        self._billing_accounts: Dict[str, BillingAccounts] = {}
        self._lock = RLock()

    def configure_accounts(self, currency: str, accounts: BillingAccounts) -> None:
        code = currency.upper()
        with self._lock:
            self._billing_accounts[code] = accounts

    def create_from_charges(
        self,
        *,
        invoice_id: str,
        client_id: str,
        currency: str,
        issue_date: date,
        due_days: int,
        charges: Iterable[FeeCharge],
        tax_rates: Mapping[str, object] = {},
    ) -> Invoice:
        if due_days < 0 or due_days > 365:
            raise ValidationError("invoice due days are invalid")
        charge_values = tuple(charges)
        if not charge_values:
            raise ValidationError("invoice requires charges")
        if any(item.client_id != client_id for item in charge_values):
            raise ValidationError("invoice charges belong to another client")
        if any(item.currency != currency.upper() for item in charge_values):
            raise ValidationError("invoice charges use another currency")
        grouped: Dict[Tuple[str, Decimal], list[FeeCharge]] = {}
        for charge in charge_values:
            rate = Decimal(str(tax_rates.get(charge.charge_type, "0")))
            if rate < 0 or rate > 1:
                raise ValidationError("invoice tax rate is invalid")
            grouped.setdefault((charge.charge_type, rate), []).append(charge)
        lines = []
        for index, ((charge_type, rate), values) in enumerate(sorted(grouped.items()), 1):
            lines.append(
                InvoiceLine(
                    "%s:%03d" % (invoice_id, index),
                    "%s charges" % charge_type.replace("_", " ").title(),
                    Decimal("1"),
                    sum((item.amount for item in values), Decimal("0")),
                    tax_rate=rate,
                    charge_ids=tuple(item.charge_id for item in values),
                )
            )
        invoice = Invoice(invoice_id, client_id, currency, issue_date, issue_date + timedelta(days=due_days), tuple(lines))
        with self._lock:
            existing = self._invoices.get(invoice.invoice_id)
            if existing is not None:
                if existing == invoice:
                    return existing
                raise StorageConflictError("invoice id already exists")
            duplicates = [item.charge_id for item in charge_values if item.charge_id in self._charge_invoice]
            if duplicates:
                raise StorageConflictError("charge is already assigned to an invoice")
            self._invoices[invoice.invoice_id] = invoice
            self._invoice_payments[invoice.invoice_id] = []
            for charge in charge_values:
                self._charge_invoice[charge.charge_id] = invoice.invoice_id
            return invoice

    def issue(self, invoice_id: str, *, issued_at: datetime, expected_version: int) -> Invoice:
        instant = utc(issued_at, "issued at")
        with self._lock:
            current = self._require_invoice(invoice_id)
            if current.version != expected_version:
                raise StorageConflictError("invoice version does not match")
            if current.status != InvoiceStatus.DRAFT:
                raise ValidationError("only draft invoices can be issued")
            changed = replace(current, status=InvoiceStatus.ISSUED, issued_at=instant, version=current.version + 1)
            if self.ledger:
                self._post_issue(changed, instant)
            self._invoices[current.invoice_id] = changed
            return changed

    def record_payment(self, payment: Payment) -> Invoice:
        if not isinstance(payment, Payment):
            raise ValidationError("payment must be Payment")
        with self._lock:
            existing = self._payments.get(payment.payment_id)
            if existing is not None:
                if existing == payment:
                    return self._invoices[payment.invoice_id]
                raise StorageConflictError("payment id already exists")
            invoice = self._require_invoice(payment.invoice_id)
            if invoice.status not in {InvoiceStatus.ISSUED, InvoiceStatus.PARTIALLY_PAID, InvoiceStatus.OVERDUE}:
                raise ValidationError("invoice cannot accept payment")
            if payment.currency != invoice.currency:
                raise ValidationError("payment currency does not match invoice")
            if payment.amount > invoice.outstanding:
                raise ValidationError("payment exceeds invoice outstanding balance")
            paid = invoice.paid_amount + payment.amount
            status = InvoiceStatus.PAID if paid == invoice.total else InvoiceStatus.PARTIALLY_PAID
            changed = replace(invoice, paid_amount=paid, status=status, version=invoice.version + 1)
            if self.ledger:
                self._post_payment(invoice, payment)
            self._payments[payment.payment_id] = payment
            self._invoice_payments[invoice.invoice_id].append(payment.payment_id)
            self._invoices[invoice.invoice_id] = changed
            return changed

    def void(self, invoice_id: str, *, reason: str, expected_version: int) -> Invoice:
        explanation = text(reason, "void reason")
        with self._lock:
            current = self._require_invoice(invoice_id)
            if current.version != expected_version:
                raise StorageConflictError("invoice version does not match")
            if current.status not in {InvoiceStatus.DRAFT, InvoiceStatus.ISSUED}:
                raise ValidationError("invoice cannot be voided")
            if current.paid_amount:
                raise ValidationError("paid invoice cannot be voided")
            changed = replace(current, status=InvoiceStatus.VOID, void_reason=explanation, version=current.version + 1)
            self._invoices[current.invoice_id] = changed
            return changed

    def mark_overdue(self, as_of: date) -> Tuple[Invoice, ...]:
        changed = []
        with self._lock:
            for invoice_id, invoice in list(self._invoices.items()):
                if invoice.due_date < as_of and invoice.status in {InvoiceStatus.ISSUED, InvoiceStatus.PARTIALLY_PAID}:
                    overdue = replace(invoice, status=InvoiceStatus.OVERDUE, version=invoice.version + 1)
                    self._invoices[invoice_id] = overdue
                    changed.append(overdue)
        return tuple(sorted(changed, key=lambda item: (item.due_date, item.invoice_id)))

    def aging(self, as_of: date) -> Tuple[AgingBucket, ...]:
        totals: Dict[Tuple[str, str], list[Decimal]] = {}
        with self._lock:
            for invoice in self._invoices.values():
                if invoice.status in {InvoiceStatus.DRAFT, InvoiceStatus.PAID, InvoiceStatus.VOID}:
                    continue
                days = (as_of - invoice.due_date).days
                index = 0 if days <= 0 else 1 if days <= 30 else 2 if days <= 60 else 3 if days <= 90 else 4
                row = totals.setdefault((invoice.client_id, invoice.currency), [Decimal("0")] * 5)
                row[index] += invoice.outstanding
        return tuple(AgingBucket(client, code, *values) for (client, code), values in sorted(totals.items()))

    def invoice(self, invoice_id: str) -> Optional[Invoice]:
        with self._lock:
            return self._invoices.get(invoice_id.strip())

    def invoices(self, *, client_id: Optional[str] = None, status: Optional[InvoiceStatus] = None) -> Tuple[Invoice, ...]:
        with self._lock:
            values = [item for item in self._invoices.values() if (client_id is None or item.client_id == client_id) and (status is None or item.status == status)]
            return tuple(sorted(values, key=lambda item: (item.issue_date, item.invoice_id)))

    def payments(self, invoice_id: str) -> Tuple[Payment, ...]:
        with self._lock:
            return tuple(self._payments[item] for item in self._invoice_payments.get(invoice_id.strip(), ()))

    def _post_issue(self, invoice: Invoice, instant: datetime) -> None:
        accounts = self._billing_accounts.get(invoice.currency)
        if accounts is None:
            raise ValidationError("billing accounts are not configured")
        lines = [JournalLine("receivable", accounts.receivable_account_id, debit=invoice.total, description=invoice.invoice_id)]
        lines.append(JournalLine("revenue", accounts.revenue_account_id, credit=invoice.subtotal, description=invoice.invoice_id))
        if invoice.tax:
            if not accounts.tax_account_id:
                raise ValidationError("tax account is not configured")
            lines.append(JournalLine("tax", accounts.tax_account_id, credit=invoice.tax, description=invoice.invoice_id))
        entry = self.ledger.create_entry(entry_id="invoice:%s" % invoice.invoice_id, journal="SALES", accounting_date=invoice.issue_date, currency=invoice.currency, description="Issue invoice %s" % invoice.invoice_id, lines=lines, created_at=instant, source="billing", source_reference=invoice.invoice_id)
        self.ledger.post(entry.entry_id, posted_at=instant, expected_version=entry.version)

    def _post_payment(self, invoice: Invoice, payment: Payment) -> None:
        accounts = self._billing_accounts.get(invoice.currency)
        if accounts is None or accounts.cash_account_id is None:
            raise ValidationError("cash billing account is not configured")
        lines = (
            JournalLine("cash", accounts.cash_account_id, debit=payment.amount, description=payment.reference),
            JournalLine("receivable", accounts.receivable_account_id, credit=payment.amount, description=invoice.invoice_id),
        )
        entry = self.ledger.create_entry(entry_id="payment:%s" % payment.payment_id, journal="CASH", accounting_date=payment.received_at.date(), currency=payment.currency, description="Payment for %s" % invoice.invoice_id, lines=lines, created_at=payment.received_at, source="billing-payment", source_reference=payment.payment_id)
        self.ledger.post(entry.entry_id, posted_at=payment.received_at, expected_version=entry.version)

    def _require_invoice(self, invoice_id: str) -> Invoice:
        value = self._invoices.get(text(invoice_id, "invoice id"))
        if value is None:
            raise NotFoundError("invoice does not exist")
        return value
