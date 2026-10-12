"""Auditable institutional trade state transitions."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from threading import RLock
from typing import Dict, Mapping, Optional, Tuple

from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError
from forex_monitor.institutional.models import (
    InstitutionalTrade,
    LifecycleEvent,
    TradeState,
    aware,
    required,
)


class TradeLifecycleService:
    _TRANSITIONS = {
        TradeState.CAPTURED: {TradeState.VALIDATED, TradeState.CANCELLED, TradeState.FAILED},
        TradeState.VALIDATED: {TradeState.CONFIRMED, TradeState.CANCELLED, TradeState.FAILED},
        TradeState.CONFIRMED: {TradeState.MATCHED, TradeState.CANCELLED, TradeState.FAILED},
        TradeState.MATCHED: {
            TradeState.SETTLEMENT_PENDING,
            TradeState.CANCELLED,
            TradeState.FAILED,
        },
        TradeState.SETTLEMENT_PENDING: {TradeState.SETTLED, TradeState.FAILED},
        TradeState.SETTLED: set(),
        TradeState.CANCELLED: set(),
        TradeState.FAILED: {TradeState.CAPTURED, TradeState.CANCELLED},
    }

    def __init__(self) -> None:
        self._trades: Dict[str, InstitutionalTrade] = {}
        self._events: Dict[str, LifecycleEvent] = {}
        self._history: Dict[str, list[LifecycleEvent]] = {}
        self._external_references: Dict[str, str] = {}
        self._lock = RLock()

    def capture(self, trade: InstitutionalTrade) -> InstitutionalTrade:
        if not isinstance(trade, InstitutionalTrade):
            raise ValidationError("trade must be InstitutionalTrade")
        with self._lock:
            existing = self._trades.get(trade.trade_id)
            if existing is not None:
                if existing == trade:
                    return existing
                raise StorageConflictError("trade id already exists")
            if trade.external_reference:
                owner = self._external_references.get(trade.external_reference)
                if owner is not None:
                    raise StorageConflictError("external trade reference already exists")
                self._external_references[trade.external_reference] = trade.trade_id
            self._trades[trade.trade_id] = trade
            self._history[trade.trade_id] = []
            return trade

    def transition(
        self,
        trade_id: str,
        target: TradeState,
        *,
        event_id: str,
        actor_id: str,
        reason: str,
        occurred_at: datetime,
        expected_version: int,
        details: Mapping[str, str] = {},
    ) -> InstitutionalTrade:
        target_state = TradeState(target)
        timestamp = aware(occurred_at, "event time")
        with self._lock:
            current = self._require(trade_id)
            existing_event = self._events.get(event_id)
            if existing_event is not None:
                if (
                    existing_event.trade_id == current.trade_id
                    and existing_event.to_state == target_state
                    and existing_event.actor_id == actor_id
                    and existing_event.reason == reason
                    and existing_event.occurred_at == timestamp
                ):
                    return current
                raise StorageConflictError("lifecycle event id already exists")
            if current.version != expected_version:
                raise StorageConflictError("trade version does not match")
            if target_state not in self._TRANSITIONS[current.state]:
                raise ValidationError(
                    "trade cannot move from %s to %s"
                    % (current.state.value, target_state.value)
                )
            history = self._history[current.trade_id]
            if history and timestamp < history[-1].occurred_at:
                raise ValidationError("lifecycle event time cannot move backward")
            event = LifecycleEvent(
                event_id=event_id,
                trade_id=current.trade_id,
                from_state=current.state,
                to_state=target_state,
                actor_id=actor_id,
                reason=reason,
                occurred_at=timestamp,
                details=tuple(details.items()),
            )
            changed = replace(current, state=target_state, version=current.version + 1)
            self._trades[current.trade_id] = changed
            self._events[event.event_id] = event
            history.append(event)
            return changed

    def validate(
        self,
        trade_id: str,
        *,
        event_id: str,
        actor_id: str,
        occurred_at: datetime,
        expected_version: int,
    ) -> InstitutionalTrade:
        current = self._require(trade_id)
        errors = []
        if current.base_amount * current.price != current.quote_amount:
            errors.append("trade amounts do not reconcile")
        if current.settlement_date < current.trade_date:
            errors.append("settlement date precedes trade date")
        if current.client_id == current.dealer_id:
            errors.append("dealer cannot be the client")
        if errors:
            raise ValidationError("; ".join(errors))
        return self.transition(
            trade_id,
            TradeState.VALIDATED,
            event_id=event_id,
            actor_id=actor_id,
            reason="economic terms validated",
            occurred_at=occurred_at,
            expected_version=expected_version,
        )

    def fail(
        self,
        trade_id: str,
        *,
        event_id: str,
        actor_id: str,
        reason: str,
        occurred_at: datetime,
        expected_version: int,
    ) -> InstitutionalTrade:
        return self.transition(
            trade_id,
            TradeState.FAILED,
            event_id=event_id,
            actor_id=actor_id,
            reason=reason,
            occurred_at=occurred_at,
            expected_version=expected_version,
        )

    def reopen(
        self,
        trade_id: str,
        *,
        event_id: str,
        actor_id: str,
        reason: str,
        occurred_at: datetime,
        expected_version: int,
    ) -> InstitutionalTrade:
        return self.transition(
            trade_id,
            TradeState.CAPTURED,
            event_id=event_id,
            actor_id=actor_id,
            reason=reason,
            occurred_at=occurred_at,
            expected_version=expected_version,
        )

    def trade(self, trade_id: str) -> Optional[InstitutionalTrade]:
        with self._lock:
            return self._trades.get(trade_id.strip())

    def by_external_reference(self, reference: str) -> Optional[InstitutionalTrade]:
        with self._lock:
            trade_id = self._external_references.get(reference.strip())
            return self._trades.get(trade_id) if trade_id else None

    def history(self, trade_id: str) -> Tuple[LifecycleEvent, ...]:
        with self._lock:
            return tuple(self._history.get(trade_id.strip(), ()))

    def list_trades(
        self,
        *,
        state: Optional[TradeState] = None,
        client_id: Optional[str] = None,
        symbol: Optional[str] = None,
        settlement_from: Optional[datetime] = None,
        settlement_to: Optional[datetime] = None,
    ) -> Tuple[InstitutionalTrade, ...]:
        state_value = TradeState(state) if state is not None else None
        symbol_value = symbol.upper().replace("/", "") if symbol else None
        with self._lock:
            values = tuple(
                item
                for item in self._trades.values()
                if (state_value is None or item.state == state_value)
                and (client_id is None or item.client_id == client_id)
                and (symbol_value is None or item.symbol == symbol_value)
                and (
                    settlement_from is None
                    or item.settlement_date >= settlement_from.date()
                )
                and (
                    settlement_to is None
                    or item.settlement_date < settlement_to.date()
                )
            )
            return tuple(sorted(values, key=lambda item: (item.booked_at, item.trade_id)))

    def state_counts(self) -> Mapping[str, int]:
        result = {state.value: 0 for state in TradeState}
        with self._lock:
            for trade in self._trades.values():
                result[trade.state.value] += 1
        return result

    def _require(self, trade_id: str) -> InstitutionalTrade:
        key = required(trade_id, "trade id")
        value = self._trades.get(key)
        if value is None:
            raise NotFoundError("trade does not exist")
        return value
