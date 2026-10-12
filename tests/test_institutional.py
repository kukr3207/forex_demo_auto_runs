from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from forex_monitor.errors import StorageConflictError, ValidationError
from forex_monitor.institutional.clients import ClientDirectory
from forex_monitor.institutional.confirmations import ConfirmationService
from forex_monitor.institutional.lifecycle import TradeLifecycleService
from forex_monitor.institutional.matching import MatchStatus, MatchingService
from forex_monitor.institutional.models import (
    AgreementStatus,
    ClientAccount,
    ClientStatus,
    InstitutionalTrade,
    LegalAgreement,
    RfqStatus,
    SettlementStatus,
    TradeState,
)
from forex_monitor.institutional.rfq import RfqService
from forex_monitor.institutional.settlement import SettlementAccount, SettlementService

UTC = timezone.utc
NOW = datetime(2026, 8, 3, 10, 0, tzinfo=UTC)


def client(**changes):
    values = {
        "client_id": "client-1",
        "legal_name": "Northwind Asset Management",
        "lei": "5493001KJTIIGC8Y1R12",
        "country": "GB",
        "base_currency": "GBP",
        "classification": "professional",
        "created_at": NOW,
    }
    values.update(changes)
    return ClientAccount(**values)


def agreement(**changes):
    values = {
        "agreement_id": "agreement-1",
        "client_id": "client-1",
        "agreement_type": "isda",
        "status": AgreementStatus.DRAFT,
        "effective_from": date(2026, 1, 1),
        "products": ("spot", "forward"),
        "currencies": ("EUR", "USD", "GBP"),
        "governing_law": "England and Wales",
    }
    values.update(changes)
    return LegalAgreement(**values)


def active_directory():
    directory = ClientDirectory()
    account = directory.create_client(client())
    account = directory.grant_permission(account.client_id, "spot", expected_version=1)
    legal = directory.add_agreement(agreement())
    legal = directory.transition_agreement(
        legal.agreement_id,
        AgreementStatus.REVIEW,
        expected_version=1,
    )
    legal = directory.transition_agreement(
        legal.agreement_id,
        AgreementStatus.EXECUTED,
        expected_version=2,
        document_hash="a" * 64,
        signed_by=("dealer-bank", "northwind"),
    )
    account = directory.transition_client(
        account.client_id,
        ClientStatus.ONBOARDING,
        expected_version=2,
    )
    account = directory.transition_client(
        account.client_id,
        ClientStatus.ACTIVE,
        expected_version=3,
    )
    return directory, account, legal


def accepted_trade():
    directory, account, _legal = active_directory()
    service = RfqService(directory)
    request = service.request(
        rfq_id="rfq-1",
        client_id=account.client_id,
        symbol="EURUSD",
        side="buy",
        notional="1000000",
        notional_currency="EUR",
        settlement_date=date(2026, 8, 5),
        requested_at=NOW,
        ttl_seconds=60,
        request_key="order-42",
    )
    request = service.quote(
        request.rfq_id,
        "1.0850",
        "dealer-1",
        expected_version=1,
        quoted_at=NOW + timedelta(seconds=2),
    )
    trade = service.accept(
        request.rfq_id,
        trade_id="trade-1",
        accepted_at=NOW + timedelta(seconds=5),
        expected_version=2,
    )
    return directory, service, trade


def validated_trade():
    _directory, _rfqs, trade = accepted_trade()
    lifecycle = TradeLifecycleService()
    lifecycle.capture(trade)
    trade = lifecycle.validate(
        trade.trade_id,
        event_id="event-validate",
        actor_id="middle-office",
        occurred_at=NOW + timedelta(minutes=1),
        expected_version=1,
    )
    return lifecycle, trade


def matched_trade():
    lifecycle, trade = validated_trade()
    confirmations = ConfirmationService(lifecycle)
    confirmation = confirmations.generate(
        trade.trade_id,
        confirmation_id="confirmation-1",
        generated_at=NOW + timedelta(minutes=2),
    )
    confirmation = confirmations.send(
        confirmation.confirmation_id,
        delivery_reference="email-1",
        sent_at=NOW + timedelta(minutes=3),
    )
    confirmations.affirm(
        confirmation.confirmation_id,
        counterparty_reference="cp-confirm-1",
        affirmed_at=NOW + timedelta(minutes=4),
        lifecycle_event_id="event-confirm",
        actor_id="client-ops",
        expected_trade_version=trade.version,
    )
    trade = lifecycle.trade(trade.trade_id)
    assert trade is not None
    matching = MatchingService(lifecycle)
    result = matching.compare(
        trade.trade_id,
        {
            "client_id": trade.client_id,
            "symbol": trade.symbol,
            "side": trade.side,
            "base_amount": str(trade.base_amount),
            "quote_amount": str(trade.quote_amount),
            "price": str(trade.price),
            "trade_date": trade.trade_date.isoformat(),
            "settlement_date": trade.settlement_date.isoformat(),
            "agreement_id": trade.agreement_id,
            "allocation_account": trade.allocation_account,
        },
        match_id="match-1",
        counterparty_reference="cp-match-1",
        received_at=NOW + timedelta(minutes=5),
    )
    assert result.status == MatchStatus.MATCHED
    trade = matching.complete_match(
        result.match_id,
        lifecycle_event_id="event-match",
        actor_id="matching-engine",
        occurred_at=NOW + timedelta(minutes=5),
        expected_trade_version=trade.version,
    )
    return lifecycle, trade


def test_client_directory_enforces_lei_uniqueness_and_versioning():
    directory = ClientDirectory()
    account = directory.create_client(client())
    assert directory.by_lei(account.lei) == account
    with pytest.raises(StorageConflictError):
        directory.create_client(client(client_id="client-2"))
    changed = directory.update_client(
        account.client_id,
        expected_version=1,
        risk_rating="high",
        settlement_accounts=("nostro-gbp",),
    )
    assert changed.version == 2
    assert changed.risk_rating == "high"
    with pytest.raises(StorageConflictError):
        directory.update_client(account.client_id, expected_version=1, legal_name="Old")


def test_client_cannot_activate_without_executed_agreement():
    directory = ClientDirectory()
    account = directory.create_client(client())
    account = directory.transition_client(
        account.client_id,
        ClientStatus.ONBOARDING,
        expected_version=1,
    )
    with pytest.raises(ValidationError, match="executed agreement"):
        directory.transition_client(
            account.client_id,
            ClientStatus.ACTIVE,
            expected_version=2,
        )


def test_executed_agreement_requires_hash_and_two_parties():
    directory = ClientDirectory()
    directory.create_client(client())
    legal = directory.add_agreement(agreement())
    legal = directory.transition_agreement(
        legal.agreement_id,
        AgreementStatus.REVIEW,
        expected_version=1,
    )
    with pytest.raises(ValidationError, match="both parties"):
        directory.transition_agreement(
            legal.agreement_id,
            AgreementStatus.EXECUTED,
            expected_version=2,
            document_hash="a" * 64,
            signed_by=("dealer",),
        )


def test_parent_permissions_are_inherited_by_active_child():
    directory, parent, _legal = active_directory()
    child = directory.create_client(
        client(
            client_id="child-1",
            lei="5493001KJTIIGC8Y1R13",
            legal_name="Northwind Fund One",
            parent_id=parent.client_id,
            status=ClientStatus.ACTIVE,
        )
    )
    assert "spot" in directory.permissions(child.client_id)
    assert directory.hierarchy(child.client_id) == (child, parent)


def test_rfq_request_keys_are_idempotent_but_terms_cannot_change():
    directory, account, _legal = active_directory()
    service = RfqService(directory)
    arguments = dict(
        rfq_id="rfq-1",
        client_id=account.client_id,
        symbol="EURUSD",
        side="buy",
        notional="1000000",
        notional_currency="EUR",
        settlement_date=date(2026, 8, 5),
        requested_at=NOW,
        request_key="client-order-1",
    )
    first = service.request(**arguments)
    assert service.request(**arguments) == first
    with pytest.raises(StorageConflictError, match="different terms"):
        service.request(**{**arguments, "rfq_id": "rfq-2", "notional": "2000000"})


def test_quote_expires_and_cannot_be_accepted_late():
    directory, account, _legal = active_directory()
    service = RfqService(directory)
    request = service.request(
        rfq_id="rfq-1",
        client_id=account.client_id,
        symbol="EURUSD",
        side="buy",
        notional="1000000",
        notional_currency="EUR",
        settlement_date=date(2026, 8, 5),
        requested_at=NOW,
        ttl_seconds=5,
    )
    request = service.quote(
        request.rfq_id,
        "1.08",
        "dealer-1",
        expected_version=1,
        quoted_at=NOW + timedelta(seconds=1),
    )
    with pytest.raises(ValidationError, match="expired"):
        service.accept(
            request.rfq_id,
            trade_id="trade-1",
            accepted_at=NOW + timedelta(seconds=6),
            expected_version=2,
        )
    assert service.request_by_id(request.rfq_id).status == RfqStatus.EXPIRED


def test_accepted_rfq_calculates_both_currency_amounts():
    _directory, service, trade = accepted_trade()
    assert trade.base_amount == Decimal("1000000")
    assert trade.quote_amount == Decimal("1085000.0000")
    assert trade.agreement_id == "agreement-1"
    assert service.dealer_statistics("dealer-1")["hit_rate"] == Decimal("1")


def test_trade_lifecycle_is_ordered_idempotent_and_versioned():
    lifecycle, trade = validated_trade()
    assert trade.state == TradeState.VALIDATED
    assert lifecycle.history(trade.trade_id)[0].event_id == "event-validate"
    duplicate = lifecycle.validate(
        trade.trade_id,
        event_id="event-validate",
        actor_id="middle-office",
        occurred_at=NOW + timedelta(minutes=1),
        expected_version=1,
    )
    assert duplicate == trade
    with pytest.raises(StorageConflictError):
        lifecycle.fail(
            trade.trade_id,
            event_id="event-fail",
            actor_id="ops",
            reason="stale request",
            occurred_at=NOW + timedelta(minutes=2),
            expected_version=1,
        )


def test_confirmation_affirmation_advances_trade_state():
    lifecycle, trade = validated_trade()
    service = ConfirmationService(lifecycle)
    confirmation = service.generate(
        trade.trade_id,
        confirmation_id="confirmation-1",
        generated_at=NOW + timedelta(minutes=2),
    )
    confirmation = service.send(
        confirmation.confirmation_id,
        delivery_reference="delivery-1",
        sent_at=NOW + timedelta(minutes=3),
    )
    affirmed = service.affirm(
        confirmation.confirmation_id,
        counterparty_reference="counterparty-1",
        affirmed_at=NOW + timedelta(minutes=4),
        lifecycle_event_id="event-confirm",
        actor_id="client-ops",
        expected_trade_version=trade.version,
    )
    assert affirmed.status.value == "affirmed"
    assert lifecycle.trade(trade.trade_id).state == TradeState.CONFIRMED


def test_confirmation_dispute_is_preserved_in_revision_history():
    lifecycle, trade = validated_trade()
    service = ConfirmationService(lifecycle)
    first = service.generate(
        trade.trade_id,
        confirmation_id="confirmation-1",
        generated_at=NOW + timedelta(minutes=2),
    )
    first = service.send(
        first.confirmation_id,
        delivery_reference="delivery-1",
        sent_at=NOW + timedelta(minutes=3),
    )
    disputed = service.dispute(
        first.confirmation_id,
        ("settlement_date", "price"),
        counterparty_reference="counterparty-1",
    )
    second = service.generate(
        trade.trade_id,
        confirmation_id="confirmation-2",
        generated_at=NOW + timedelta(minutes=4),
    )
    assert disputed.status.value == "disputed"
    assert second.revision == 2
    assert len(service.history(trade.trade_id)) == 2


def test_matching_reports_all_field_discrepancies():
    lifecycle, trade = validated_trade()
    trade = lifecycle.transition(
        trade.trade_id,
        TradeState.CONFIRMED,
        event_id="event-confirm",
        actor_id="ops",
        reason="manual confirmation",
        occurred_at=NOW + timedelta(minutes=2),
        expected_version=trade.version,
    )
    service = MatchingService(lifecycle)
    result = service.compare(
        trade.trade_id,
        {
            "client_id": trade.client_id,
            "symbol": trade.symbol,
            "side": "sell",
            "base_amount": str(trade.base_amount),
            "quote_amount": "1085100",
            "price": "1.0851",
            "trade_date": trade.trade_date.isoformat(),
            "settlement_date": trade.settlement_date.isoformat(),
            "agreement_id": trade.agreement_id,
            "allocation_account": trade.allocation_account,
        },
        match_id="match-1",
        counterparty_reference="cp-match-1",
        received_at=NOW + timedelta(minutes=3),
        price_tolerance="0.0002",
        amount_tolerance="10",
    )
    assert result.status == MatchStatus.MISMATCHED
    mismatch_names = {field.name for field in result.fields if not field.matched}
    assert mismatch_names == {"side", "quote_amount"}
    with pytest.raises(ValidationError, match="identity mismatches"):
        service.override(
            result.match_id,
            actor_id="supervisor",
            reason="accept break",
            expected_version=1,
            resolved_at=NOW + timedelta(minutes=4),
        )


def test_settlement_requires_two_acknowledged_legs_before_completion():
    lifecycle, trade = matched_trade()
    service = SettlementService(lifecycle)
    service.register_account(
        SettlementAccount("usd-account", trade.client_id, "USD", "BANKUS", "USD-1")
    )
    service.register_account(
        SettlementAccount("eur-account", trade.client_id, "EUR", "BANKEU", "EUR-1")
    )
    service.create_instructions(
        trade.trade_id,
        pay_instruction_id="instruction-pay",
        receive_instruction_id="instruction-receive",
        account_ids={"USD": "usd-account", "EUR": "eur-account"},
    )
    released = service.release(
        trade.trade_id,
        released_at=NOW + timedelta(minutes=6),
        lifecycle_event_id="event-instruct",
        actor_id="settlements",
        expected_trade_version=trade.version,
    )
    assert all(item.status == SettlementStatus.RELEASED for item in released)
    settled = []
    for index, item in enumerate(released):
        item = service.acknowledge(
            item.instruction_id,
            "network-%s" % index,
            expected_version=item.version,
        )
        item = service.settle(
            item.instruction_id,
            settled_at=NOW + timedelta(minutes=10 + index),
            expected_version=item.version,
        )
        settled.append(item)
    pending_trade = lifecycle.trade(trade.trade_id)
    completed = service.complete_trade(
        trade.trade_id,
        lifecycle_event_id="event-settle",
        actor_id="settlements",
        occurred_at=NOW + timedelta(minutes=12),
        expected_trade_version=pending_trade.version,
    )
    assert completed.state == TradeState.SETTLED
    assert service.cash_ladder(trade.settlement_date) == {
        "USD": -trade.quote_amount,
        "EUR": trade.base_amount,
    }


def test_failed_settlement_can_retry_without_losing_instruction_identity():
    lifecycle, trade = matched_trade()
    service = SettlementService(lifecycle)
    service.register_account(
        SettlementAccount("usd-account", trade.client_id, "USD", "BANKUS", "USD-1")
    )
    service.register_account(
        SettlementAccount("eur-account", trade.client_id, "EUR", "BANKEU", "EUR-1")
    )
    service.create_instructions(
        trade.trade_id,
        pay_instruction_id="instruction-pay",
        receive_instruction_id="instruction-receive",
        account_ids={"USD": "usd-account", "EUR": "eur-account"},
    )
    pay = service.instructions_for(trade.trade_id)[0]
    failed = service.fail(pay.instruction_id, "bank unavailable", expected_version=pay.version)
    retried = service.retry(failed.instruction_id, expected_version=failed.version)
    assert retried.instruction_id == pay.instruction_id
    assert retried.status == SettlementStatus.DRAFT
    assert retried.failure_reason is None
