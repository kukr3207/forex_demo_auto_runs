"""Thread-safe multi-currency general ledger with immutable postings."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from threading import RLock
from typing import Dict, Iterable, Mapping, Optional, Tuple

from forex_monitor.accounting.models import Account, EntryStatus, JournalEntry, JournalLine, text, utc
from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError


@dataclass(frozen=True)
class AccountBalance:
    account_id: str
    currency: str
    debits: Decimal
    credits: Decimal
    balance: Decimal
    as_of: date


@dataclass(frozen=True)
class TrialBalance:
    as_of: date
    currency: str
    accounts: Tuple[AccountBalance, ...]

    @property
    def debit_total(self) -> Decimal:
        return sum((max(item.balance, Decimal("0")) for item in self.accounts), Decimal("0"))

    @property
    def credit_total(self) -> Decimal:
        return sum((max(-item.balance, Decimal("0")) for item in self.accounts), Decimal("0"))

    @property
    def balanced(self) -> bool:
        raw_debits = sum((item.debits for item in self.accounts), Decimal("0"))
        raw_credits = sum((item.credits for item in self.accounts), Decimal("0"))
        return raw_debits == raw_credits


class GeneralLedger:
    def __init__(self) -> None:
        self._accounts: Dict[str, Account] = {}
        self._codes: Dict[Tuple[str, str], str] = {}
        self._entries: Dict[str, JournalEntry] = {}
        self._source_index: Dict[Tuple[str, str], str] = {}
        self._lock = RLock()

    def add_account(self, account: Account) -> Account:
        if not isinstance(account, Account):
            raise ValidationError("account must be Account")
        with self._lock:
            existing = self._accounts.get(account.account_id)
            if existing is not None:
                if existing == account:
                    return existing
                raise StorageConflictError("account id already exists")
            code_key = (account.code, account.currency)
            if code_key in self._codes:
                raise StorageConflictError("account code and currency already exist")
            if account.parent_id:
                parent = self._accounts.get(account.parent_id)
                if parent is None:
                    raise ValidationError("parent account does not exist")
                if parent.currency != account.currency:
                    raise ValidationError("parent and child account currencies differ")
            self._accounts[account.account_id] = account
            self._codes[code_key] = account.account_id
            return account

    def deactivate_account(self, account_id: str, *, expected_version: int) -> Account:
        with self._lock:
            current = self._require_account(account_id)
            if current.version != expected_version:
                raise StorageConflictError("account version does not match")
            if any(item.parent_id == current.account_id and item.active for item in self._accounts.values()):
                raise ValidationError("account has active child accounts")
            changed = replace(current, active=False, version=current.version + 1)
            self._accounts[current.account_id] = changed
            return changed

    def create_entry(
        self,
        *,
        entry_id: str,
        journal: str,
        accounting_date: date,
        currency: str,
        description: str,
        lines: Iterable[JournalLine],
        created_at: datetime,
        source: str = "manual",
        source_reference: Optional[str] = None,
    ) -> JournalEntry:
        entry = JournalEntry(
            entry_id,
            journal,
            accounting_date,
            currency,
            description,
            tuple(lines),
            created_at,
            source=source,
            source_reference=source_reference,
        )
        with self._lock:
            existing = self._entries.get(entry.entry_id)
            if existing is not None:
                if existing == entry:
                    return existing
                raise StorageConflictError("journal entry id already exists")
            if source_reference:
                source_key = (entry.source, source_reference)
                duplicate = self._source_index.get(source_key)
                if duplicate:
                    candidate = self._entries[duplicate]
                    if candidate == entry:
                        return candidate
                    raise StorageConflictError("source reference already has a journal entry")
            self._validate_accounts(entry)
            self._entries[entry.entry_id] = entry
            if source_reference:
                self._source_index[(entry.source, source_reference)] = entry.entry_id
            return entry

    def post(self, entry_id: str, *, posted_at: datetime, expected_version: int) -> JournalEntry:
        instant = utc(posted_at, "posted at")
        with self._lock:
            current = self._require_entry(entry_id)
            if current.version != expected_version:
                raise StorageConflictError("journal entry version does not match")
            if current.status != EntryStatus.DRAFT:
                raise ValidationError("only draft entries can be posted")
            self._validate_accounts(current)
            changed = replace(current, status=EntryStatus.POSTED, posted_at=instant, version=current.version + 1)
            self._entries[current.entry_id] = changed
            return changed

    def reverse(
        self,
        entry_id: str,
        *,
        reversal_id: str,
        accounting_date: date,
        created_at: datetime,
        posted_at: datetime,
    ) -> JournalEntry:
        with self._lock:
            original = self._require_entry(entry_id)
            if any(item.reversal_of == original.entry_id for item in self._entries.values()):
                existing = next(item for item in self._entries.values() if item.reversal_of == original.entry_id)
                if existing.entry_id == reversal_id:
                    return existing
                raise StorageConflictError("journal entry is already reversed")
            if original.status != EntryStatus.POSTED:
                raise ValidationError("only posted entries can be reversed")
            lines = tuple(
                JournalLine(
                    line_id="%s:R" % line.line_id,
                    account_id=line.account_id,
                    debit=line.credit,
                    credit=line.debit,
                    description="Reversal: %s" % line.description,
                    dimensions=line.dimensions,
                )
                for line in original.lines
            )
            reversal = JournalEntry(
                reversal_id,
                original.journal,
                accounting_date,
                original.currency,
                "Reversal of %s: %s" % (original.entry_id, original.description),
                lines,
                created_at,
                status=EntryStatus.POSTED,
                source="reversal",
                source_reference=original.entry_id,
                posted_at=posted_at,
                reversal_of=original.entry_id,
            )
            self._validate_accounts(reversal)
            self._entries[reversal.entry_id] = reversal
            self._source_index[("reversal", original.entry_id)] = reversal.entry_id
            self._entries[original.entry_id] = replace(original, status=EntryStatus.REVERSED, version=original.version + 1)
            return reversal

    def account_balance(self, account_id: str, *, as_of: date) -> AccountBalance:
        with self._lock:
            account = self._require_account(account_id)
            debits = Decimal("0")
            credits = Decimal("0")
            for entry in self._entries.values():
                if entry.status not in {EntryStatus.POSTED, EntryStatus.REVERSED} or entry.accounting_date > as_of:
                    continue
                for line in entry.lines:
                    if line.account_id == account.account_id:
                        debits += line.debit
                        credits += line.credit
            raw = debits - credits
            balance = raw if account.normal_side == "debit" else -raw
            return AccountBalance(account.account_id, account.currency, debits, credits, balance, as_of)

    def trial_balance(self, *, as_of: date, currency: str) -> TrialBalance:
        code = currency.upper()
        with self._lock:
            balances = tuple(
                self.account_balance(account.account_id, as_of=as_of)
                for account in sorted(self._accounts.values(), key=lambda item: item.code)
                if account.currency == code
            )
            return TrialBalance(as_of, code, balances)

    def entries(
        self,
        *,
        status: Optional[EntryStatus] = None,
        journal: Optional[str] = None,
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
    ) -> Tuple[JournalEntry, ...]:
        with self._lock:
            values = [
                item for item in self._entries.values()
                if (status is None or item.status == status)
                and (journal is None or item.journal == journal.upper())
                and (from_date is None or item.accounting_date >= from_date)
                and (to_date is None or item.accounting_date <= to_date)
            ]
            return tuple(sorted(values, key=lambda item: (item.accounting_date, item.entry_id)))

    def account(self, account_id: str) -> Optional[Account]:
        with self._lock:
            return self._accounts.get(account_id.strip())

    def entry(self, entry_id: str) -> Optional[JournalEntry]:
        with self._lock:
            return self._entries.get(entry_id.strip())

    def _validate_accounts(self, entry: JournalEntry) -> None:
        for line in entry.lines:
            account = self._accounts.get(line.account_id)
            if account is None:
                raise ValidationError("journal line references an unknown account")
            if not account.active:
                raise ValidationError("journal line references an inactive account")
            if account.currency != entry.currency:
                raise ValidationError("journal line account currency differs from entry")
            if entry.source == "manual" and not account.allow_manual_posting:
                raise ValidationError("account does not allow manual postings")

    def _require_account(self, account_id: str) -> Account:
        value = self._accounts.get(text(account_id, "account id"))
        if value is None:
            raise NotFoundError("ledger account does not exist")
        return value

    def _require_entry(self, entry_id: str) -> JournalEntry:
        value = self._entries.get(text(entry_id, "entry id"))
        if value is None:
            raise NotFoundError("journal entry does not exist")
        return value
