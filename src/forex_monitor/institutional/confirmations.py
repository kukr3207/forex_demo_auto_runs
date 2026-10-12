"""Versioned trade confirmations and counterparty affirmation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime
from threading import RLock
from typing import Dict, Iterable, Mapping, Optional, Tuple

from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError
from forex_monitor.institutional.lifecycle import TradeLifecycleService
from forex_monitor.institutional.models import (
    Confirmation,
    ConfirmationStatus,
    InstitutionalTrade,
    TradeState,
    aware,
    required,
)


class ConfirmationService:
    def __init__(self, lifecycle: TradeLifecycleService) -> None:
        self._lifecycle = lifecycle
        self._confirmations: Dict[str, Confirmation] = {}
        self._trade_revisions: Dict[str, list[str]] = {}
        self._delivery_references: Dict[str, str] = {}
        self._lock = RLock()

    def generate(
        self,
        trade_id: str,
        *,
        confirmation_id: str,
        generated_at: datetime,
    ) -> Confirmation:
        trade = self._require_trade(trade_id)
        if trade.state not in {TradeState.VALIDATED, TradeState.CONFIRMED}:
            raise ValidationError("trade must be validated before confirmation")
        with self._lock:
            existing = self._confirmations.get(confirmation_id)
            revisions = self._trade_revisions.setdefault(trade.trade_id, [])
            revision = len(revisions) + 1
            value = Confirmation(
                confirmation_id=confirmation_id,
                trade_id=trade.trade_id,
                revision=revision,
                status=ConfirmationStatus.DRAFT,
                generated_at=generated_at,
                terms_hash=self._terms_hash(trade),
            )
            if existing is not None:
                if existing == value:
                    return existing
                raise StorageConflictError("confirmation id already exists")
            if revisions:
                previous = self._confirmations[revisions[-1]]
                if previous.status not in {
                    ConfirmationStatus.DISPUTED,
                    ConfirmationStatus.SUPERSEDED,
                }:
                    self._confirmations[previous.confirmation_id] = replace(
                        previous,
                        status=ConfirmationStatus.SUPERSEDED,
                    )
            self._confirmations[value.confirmation_id] = value
            revisions.append(value.confirmation_id)
            return value

    def send(
        self,
        confirmation_id: str,
        *,
        delivery_reference: str,
        sent_at: datetime,
    ) -> Confirmation:
        reference = required(delivery_reference, "delivery reference")
        timestamp = aware(sent_at, "sent at")
        with self._lock:
            current = self._require(confirmation_id)
            existing_id = self._delivery_references.get(reference)
            if existing_id:
                existing = self._confirmations[existing_id]
                if existing.confirmation_id == current.confirmation_id:
                    return existing
                raise StorageConflictError("delivery reference already exists")
            if current.status != ConfirmationStatus.DRAFT:
                raise ValidationError("only draft confirmations can be sent")
            if timestamp < current.generated_at:
                raise ValidationError("confirmation cannot be sent before generation")
            changed = replace(current, status=ConfirmationStatus.SENT, sent_at=timestamp)
            self._confirmations[current.confirmation_id] = changed
            self._delivery_references[reference] = current.confirmation_id
            return changed

    def affirm(
        self,
        confirmation_id: str,
        *,
        counterparty_reference: str,
        affirmed_at: datetime,
        lifecycle_event_id: str,
        actor_id: str,
        expected_trade_version: int,
    ) -> Confirmation:
        timestamp = aware(affirmed_at, "affirmed at")
        with self._lock:
            current = self._require(confirmation_id)
            if current.status == ConfirmationStatus.AFFIRMED:
                if current.counterparty_reference == counterparty_reference:
                    return current
                raise StorageConflictError("confirmation has another affirmation reference")
            if current.status != ConfirmationStatus.SENT:
                raise ValidationError("only sent confirmations can be affirmed")
            if current.sent_at is not None and timestamp < current.sent_at:
                raise ValidationError("affirmation cannot predate delivery")
            trade = self._require_trade(current.trade_id)
            if self._terms_hash(trade) != current.terms_hash:
                raise StorageConflictError("trade terms changed after confirmation generation")
            self._lifecycle.transition(
                trade.trade_id,
                TradeState.CONFIRMED,
                event_id=lifecycle_event_id,
                actor_id=actor_id,
                reason="counterparty affirmed confirmation",
                occurred_at=timestamp,
                expected_version=expected_trade_version,
                details={"confirmation_id": current.confirmation_id},
            )
            changed = replace(
                current,
                status=ConfirmationStatus.AFFIRMED,
                affirmed_at=timestamp,
                counterparty_reference=required(
                    counterparty_reference,
                    "counterparty reference",
                ),
            )
            self._confirmations[current.confirmation_id] = changed
            return changed

    def dispute(
        self,
        confirmation_id: str,
        fields: Iterable[str],
        *,
        counterparty_reference: str,
    ) -> Confirmation:
        disputed = tuple(sorted(set(required(item, "disputed field") for item in fields)))
        if not disputed:
            raise ValidationError("confirmation dispute requires at least one field")
        with self._lock:
            current = self._require(confirmation_id)
            if current.status not in {ConfirmationStatus.SENT, ConfirmationStatus.AFFIRMED}:
                raise ValidationError("confirmation cannot be disputed in its current state")
            changed = replace(
                current,
                status=ConfirmationStatus.DISPUTED,
                disputed_fields=disputed,
                counterparty_reference=required(
                    counterparty_reference,
                    "counterparty reference",
                ),
            )
            self._confirmations[current.confirmation_id] = changed
            return changed

    def latest(self, trade_id: str) -> Optional[Confirmation]:
        with self._lock:
            revisions = self._trade_revisions.get(trade_id.strip(), ())
            return self._confirmations[revisions[-1]] if revisions else None

    def history(self, trade_id: str) -> Tuple[Confirmation, ...]:
        with self._lock:
            return tuple(
                self._confirmations[item]
                for item in self._trade_revisions.get(trade_id.strip(), ())
            )

    def pending(self) -> Tuple[Confirmation, ...]:
        with self._lock:
            values = (
                item
                for item in self._confirmations.values()
                if item.status in {ConfirmationStatus.DRAFT, ConfirmationStatus.SENT}
            )
            return tuple(sorted(values, key=lambda item: item.generated_at))

    def _require(self, confirmation_id: str) -> Confirmation:
        key = required(confirmation_id, "confirmation id")
        value = self._confirmations.get(key)
        if value is None:
            raise NotFoundError("confirmation does not exist")
        return value

    def _require_trade(self, trade_id: str) -> InstitutionalTrade:
        trade = self._lifecycle.trade(trade_id)
        if trade is None:
            raise NotFoundError("trade does not exist")
        return trade

    @staticmethod
    def _terms_hash(trade: InstitutionalTrade) -> str:
        terms = {
            "trade_id": trade.trade_id,
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
        }
        payload = json.dumps(terms, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
