"""Economic term matching and discrepancy management."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from enum import Enum
from threading import RLock
from typing import Dict, Mapping, Optional, Tuple

from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError
from forex_monitor.institutional.lifecycle import TradeLifecycleService
from forex_monitor.institutional.models import InstitutionalTrade, TradeState, aware, required


class MatchStatus(str, Enum):
    PENDING = "pending"
    MATCHED = "matched"
    MISMATCHED = "mismatched"
    OVERRIDDEN = "overridden"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class MatchField:
    name: str
    local_value: str
    counterparty_value: str
    matched: bool
    tolerance: Optional[Decimal] = None
    difference: Optional[Decimal] = None


@dataclass(frozen=True)
class MatchResult:
    match_id: str
    trade_id: str
    status: MatchStatus
    counterparty_reference: str
    fields: Tuple[MatchField, ...]
    received_at: datetime
    resolved_at: Optional[datetime] = None
    resolved_by: Optional[str] = None
    resolution_reason: Optional[str] = None
    version: int = 1


class MatchingService:
    _FIELDS = (
        "client_id",
        "symbol",
        "side",
        "base_amount",
        "quote_amount",
        "price",
        "trade_date",
        "settlement_date",
        "agreement_id",
        "allocation_account",
    )

    def __init__(self, lifecycle: TradeLifecycleService) -> None:
        self._lifecycle = lifecycle
        self._matches: Dict[str, MatchResult] = {}
        self._trade_matches: Dict[str, list[str]] = {}
        self._counterparty_references: Dict[str, str] = {}
        self._lock = RLock()

    def compare(
        self,
        trade_id: str,
        counterparty_terms: Mapping[str, object],
        *,
        match_id: str,
        counterparty_reference: str,
        received_at: datetime,
        price_tolerance: object = "0",
        amount_tolerance: object = "0",
    ) -> MatchResult:
        trade = self._require_trade(trade_id)
        if trade.state != TradeState.CONFIRMED:
            raise ValidationError("trade must be confirmed before matching")
        price_limit = self._non_negative(price_tolerance, "price tolerance")
        amount_limit = self._non_negative(amount_tolerance, "amount tolerance")
        reference = required(counterparty_reference, "counterparty reference")
        timestamp = aware(received_at, "received at")
        with self._lock:
            existing = self._matches.get(match_id)
            if existing is not None:
                if (
                    existing.trade_id == trade.trade_id
                    and existing.counterparty_reference == reference
                ):
                    return existing
                raise StorageConflictError("match id already exists")
            owner = self._counterparty_references.get(reference)
            if owner is not None:
                raise StorageConflictError("counterparty match reference already exists")
            fields = tuple(
                self._compare_field(
                    trade,
                    field_name,
                    counterparty_terms.get(field_name),
                    price_limit,
                    amount_limit,
                )
                for field_name in self._FIELDS
            )
            status = (
                MatchStatus.MATCHED
                if all(field.matched for field in fields)
                else MatchStatus.MISMATCHED
            )
            result = MatchResult(
                match_id=required(match_id, "match id"),
                trade_id=trade.trade_id,
                status=status,
                counterparty_reference=reference,
                fields=fields,
                received_at=timestamp,
            )
            self._matches[result.match_id] = result
            self._trade_matches.setdefault(trade.trade_id, []).append(result.match_id)
            self._counterparty_references[reference] = result.match_id
            return result

    def complete_match(
        self,
        match_id: str,
        *,
        lifecycle_event_id: str,
        actor_id: str,
        occurred_at: datetime,
        expected_trade_version: int,
    ) -> InstitutionalTrade:
        with self._lock:
            current = self._require(match_id)
            if current.status not in {MatchStatus.MATCHED, MatchStatus.OVERRIDDEN}:
                raise ValidationError("unresolved mismatch cannot complete matching")
            return self._lifecycle.transition(
                current.trade_id,
                TradeState.MATCHED,
                event_id=lifecycle_event_id,
                actor_id=actor_id,
                reason="trade terms matched",
                occurred_at=occurred_at,
                expected_version=expected_trade_version,
                details={"match_id": current.match_id},
            )

    def override(
        self,
        match_id: str,
        *,
        actor_id: str,
        reason: str,
        expected_version: int,
        resolved_at: datetime,
    ) -> MatchResult:
        with self._lock:
            current = self._require(match_id)
            if current.version != expected_version:
                raise StorageConflictError("match version does not match")
            if current.status != MatchStatus.MISMATCHED:
                raise ValidationError("only mismatched results can be overridden")
            mismatches = [field for field in current.fields if not field.matched]
            if any(field.name in {"client_id", "symbol", "side"} for field in mismatches):
                raise ValidationError("identity mismatches cannot be overridden")
            changed = replace(
                current,
                status=MatchStatus.OVERRIDDEN,
                resolved_at=aware(resolved_at, "resolved at"),
                resolved_by=required(actor_id, "actor id"),
                resolution_reason=required(reason, "resolution reason"),
                version=current.version + 1,
            )
            self._matches[current.match_id] = changed
            return changed

    def cancel(
        self,
        match_id: str,
        *,
        actor_id: str,
        reason: str,
        expected_version: int,
        resolved_at: datetime,
    ) -> MatchResult:
        with self._lock:
            current = self._require(match_id)
            if current.version != expected_version:
                raise StorageConflictError("match version does not match")
            if current.status == MatchStatus.CANCELLED:
                return current
            changed = replace(
                current,
                status=MatchStatus.CANCELLED,
                resolved_at=aware(resolved_at, "resolved at"),
                resolved_by=required(actor_id, "actor id"),
                resolution_reason=required(reason, "resolution reason"),
                version=current.version + 1,
            )
            self._matches[current.match_id] = changed
            return changed

    def result(self, match_id: str) -> Optional[MatchResult]:
        with self._lock:
            return self._matches.get(match_id.strip())

    def history(self, trade_id: str) -> Tuple[MatchResult, ...]:
        with self._lock:
            return tuple(
                self._matches[item]
                for item in self._trade_matches.get(trade_id.strip(), ())
            )

    def breaks(self) -> Tuple[MatchResult, ...]:
        with self._lock:
            values = (
                item
                for item in self._matches.values()
                if item.status == MatchStatus.MISMATCHED
            )
            return tuple(sorted(values, key=lambda item: item.received_at))

    @classmethod
    def _compare_field(
        cls,
        trade: InstitutionalTrade,
        name: str,
        counterparty: object,
        price_tolerance: Decimal,
        amount_tolerance: Decimal,
    ) -> MatchField:
        local = getattr(trade, name)
        local_text = (
            ""
            if local is None
            else local.isoformat()
            if hasattr(local, "isoformat")
            else str(local)
        )
        counterparty_text = "" if counterparty is None else str(counterparty)
        tolerance = None
        difference = None
        if name in {"base_amount", "quote_amount", "price"}:
            try:
                counterparty_number = Decimal(counterparty_text)
            except Exception:
                return MatchField(name, local_text, counterparty_text, False)
            local_number = Decimal(local_text)
            difference = abs(local_number - counterparty_number)
            tolerance = price_tolerance if name == "price" else amount_tolerance
            matched = difference <= tolerance
        else:
            matched = local_text == counterparty_text
        return MatchField(
            name=name,
            local_value=local_text,
            counterparty_value=counterparty_text,
            matched=matched,
            tolerance=tolerance,
            difference=difference,
        )

    def _require(self, match_id: str) -> MatchResult:
        value = self._matches.get(required(match_id, "match id"))
        if value is None:
            raise NotFoundError("match result does not exist")
        return value

    def _require_trade(self, trade_id: str) -> InstitutionalTrade:
        value = self._lifecycle.trade(trade_id)
        if value is None:
            raise NotFoundError("trade does not exist")
        return value

    @staticmethod
    def _non_negative(value: object, field_name: str) -> Decimal:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0:
            raise ValidationError("%s must be non-negative" % field_name)
        return result
