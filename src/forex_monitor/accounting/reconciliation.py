"""Ledger-to-bank and subledger-to-control-account reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable, Optional, Tuple

from forex_monitor.accounting.ledger import GeneralLedger
from forex_monitor.accounting.models import Invoice, InvoiceStatus, money, text, utc
from forex_monitor.errors import ValidationError


@dataclass(frozen=True)
class BankTransaction:
    transaction_id: str
    booked_at: datetime
    amount: Decimal
    currency: str
    reference: str
    counterparty: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "transaction_id", text(self.transaction_id, "bank transaction id"))
        object.__setattr__(self, "booked_at", utc(self.booked_at, "booked at"))
        amount = money(self.amount, "bank amount")
        if amount == 0:
            raise ValidationError("bank amount cannot be zero")
        object.__setattr__(self, "amount", amount)
        code = self.currency.upper()
        if len(code) != 3:
            raise ValidationError("bank currency is invalid")
        object.__setattr__(self, "currency", code)
        object.__setattr__(self, "reference", text(self.reference, "bank reference"))


@dataclass(frozen=True)
class ReconciliationMatch:
    bank_transaction_id: str
    ledger_entry_id: str
    amount: Decimal
    rule: str
    confidence: Decimal


@dataclass(frozen=True)
class ReconciliationResult:
    matches: Tuple[ReconciliationMatch, ...]
    unmatched_bank_ids: Tuple[str, ...]
    unmatched_ledger_ids: Tuple[str, ...]
    bank_total: Decimal
    ledger_total: Decimal

    @property
    def difference(self) -> Decimal:
        return self.bank_total - self.ledger_total


@dataclass(frozen=True)
class ControlAccountResult:
    control_balance: Decimal
    subledger_balance: Decimal
    difference: Decimal
    invoice_count: int


class LedgerReconciler:
    def __init__(self, *, amount_tolerance: object = "0.01", day_tolerance: int = 3) -> None:
        self.amount_tolerance = money(amount_tolerance, "amount tolerance", non_negative=True)
        if day_tolerance < 0 or day_tolerance > 31:
            raise ValidationError("day tolerance is invalid")
        self.day_tolerance = day_tolerance

    def reconcile_bank(
        self,
        bank: Iterable[BankTransaction],
        ledger: GeneralLedger,
        *,
        journal: str,
        from_date: date,
        to_date: date,
    ) -> ReconciliationResult:
        bank_values = tuple(bank)
        entries = tuple(item for item in ledger.entries(journal=journal, from_date=from_date, to_date=to_date) if item.status.value in {"posted", "reversed"})
        available = {item.entry_id: item for item in entries}
        matches = []
        unmatched_bank = []
        for transaction in sorted(bank_values, key=lambda item: (item.booked_at, item.transaction_id)):
            candidates = []
            reference = transaction.reference.lower()
            for entry in available.values():
                if entry.currency != transaction.currency:
                    continue
                if abs(entry.amount - abs(transaction.amount)) > self.amount_tolerance:
                    continue
                days = abs((entry.accounting_date - transaction.booked_at.date()).days)
                if days > self.day_tolerance:
                    continue
                reference_match = bool(reference and (reference in entry.description.lower() or reference == (entry.source_reference or "").lower()))
                confidence = Decimal("1") if reference_match else Decimal("0.8") - Decimal(days) * Decimal("0.05")
                candidates.append((confidence, -days, entry.entry_id, entry))
            if not candidates:
                unmatched_bank.append(transaction.transaction_id)
                continue
            candidates.sort(reverse=True, key=lambda item: item[:3])
            confidence, _, _, selected = candidates[0]
            matches.append(ReconciliationMatch(transaction.transaction_id, selected.entry_id, abs(transaction.amount), "reference_amount_date" if confidence == 1 else "amount_date", confidence))
            available.pop(selected.entry_id)
        bank_total = sum((abs(item.amount) for item in bank_values), Decimal("0"))
        ledger_total = sum((item.amount for item in entries), Decimal("0"))
        return ReconciliationResult(tuple(matches), tuple(unmatched_bank), tuple(sorted(available)), bank_total, ledger_total)

    @staticmethod
    def reconcile_receivables(
        invoices: Iterable[Invoice],
        ledger: GeneralLedger,
        *,
        receivable_account_id: str,
        as_of: date,
    ) -> ControlAccountResult:
        values = tuple(item for item in invoices if item.issue_date <= as_of and item.status not in {InvoiceStatus.DRAFT, InvoiceStatus.VOID})
        subledger = sum((item.outstanding for item in values), Decimal("0"))
        control = ledger.account_balance(receivable_account_id, as_of=as_of).balance
        return ControlAccountResult(control, subledger, control - subledger, len(values))
