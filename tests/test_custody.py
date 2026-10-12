from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from forex_monitor.custody.accounts import CustodyLedger
from forex_monitor.custody.models import CashMovement, CashReservation, CashTransfer, CustodyAccount, MovementType, TransferState
from forex_monitor.custody.statements import StatementService
from forex_monitor.custody.transfers import TransferService
from forex_monitor.errors import ValidationError


NOW = datetime(2026, 10, 12, 8, tzinfo=timezone.utc)


def ledger():
    value = CustodyLedger()
    value.open_account(CustodyAccount("source", "client", "USD", "Source"), opened_at=NOW)
    value.open_account(CustodyAccount("destination", "client", "USD", "Destination"), opened_at=NOW)
    value.post(CashMovement("deposit", "source", MovementType.DEPOSIT, 1000, NOW, NOW.date(), "bank", "deposit"))
    return value


def test_reservation_reduces_available_cash_and_can_expire() -> None:
    book = ledger()
    reservation = book.reserve(CashReservation("reserve", "source", 400, "order", "order-1", NOW, NOW + timedelta(hours=1)))
    assert book.balance("source", as_of=NOW).available == Decimal("600")
    book.expire_reservations(NOW + timedelta(hours=2))
    assert book.balance("source", as_of=NOW + timedelta(hours=2)).available == Decimal("1000")


def test_internal_transfer_requires_independent_approval_and_settles() -> None:
    book = ledger()
    transfers = TransferService(book)
    transfer = transfers.create(CashTransfer("transfer", "source", "destination", 250, "USD", NOW, "alice"))
    with pytest.raises(ValidationError):
        transfers.approve(transfer.transfer_id, approver="alice")
    approved = transfers.approve(transfer.transfer_id, approver="bob")
    reserved = transfers.reserve(transfer.transfer_id, at=NOW, expected_version=approved.version)
    settled = transfers.settle(transfer.transfer_id, at=NOW, expected_version=reserved.version)
    assert settled.state == TransferState.SETTLED
    assert book.balance("source", as_of=NOW).booked == Decimal("750")
    assert book.balance("destination", as_of=NOW).booked == Decimal("250")


def test_transfer_rejects_insufficient_cash_without_state_change() -> None:
    book = ledger()
    transfers = TransferService(book)
    transfer = transfers.create(CashTransfer("transfer", "source", "destination", 2000, "USD", NOW, "alice"))
    approved = transfers.approve(transfer.transfer_id, approver="bob")
    with pytest.raises(ValidationError):
        transfers.reserve(transfer.transfer_id, at=NOW, expected_version=approved.version)
    assert transfers.transfers()[0].state == TransferState.APPROVED


def test_cash_statement_reconciles_opening_movements_and_closing() -> None:
    book = ledger()
    book.post(CashMovement("interest", "source", MovementType.INTEREST, 10, NOW + timedelta(hours=1), NOW.date(), "interest", "one"))
    statement = StatementService(book).generate(statement_id="statement", account_id="source", start=NOW, end=NOW + timedelta(hours=2), generated_at=NOW + timedelta(hours=2))
    assert statement.reconciled
    assert statement.opening_balance == Decimal("1000")
    assert statement.closing_balance == Decimal("1010")
