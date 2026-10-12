"""Deterministic retry queue with backoff, leases, and dead letters."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Dict, Mapping, Optional, Sequence, Tuple

from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError
from forex_monitor.models import ensure_utc


@dataclass(frozen=True)
class WorkItem:
    item_id: str
    kind: str
    payload: Mapping[str, object]
    available_at: datetime
    attempts: int = 0
    maximum_attempts: int = 5
    leased_until: Optional[datetime] = None
    lease_owner: Optional[str] = None
    last_error: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.item_id.strip() or not self.kind.strip():
            raise ValidationError("work item id and kind cannot be blank")
        if self.attempts < 0 or self.maximum_attempts <= 0 or self.attempts > self.maximum_attempts:
            raise ValidationError("work item attempt counts are invalid")
        object.__setattr__(self, "available_at", ensure_utc(self.available_at, "availability time"))
        if self.leased_until:
            object.__setattr__(self, "leased_until", ensure_utc(self.leased_until, "lease expiry"))


@dataclass(frozen=True)
class DeadLetter:
    item: WorkItem
    failed_at: datetime
    reason: str


class RetryQueue:
    def __init__(self, *, base_delay: timedelta = timedelta(seconds=1), maximum_delay: timedelta = timedelta(minutes=15)) -> None:
        if base_delay.total_seconds() <= 0 or maximum_delay < base_delay:
            raise ValidationError("retry delays are invalid")
        self.base_delay = base_delay
        self.maximum_delay = maximum_delay
        self._items: Dict[str, WorkItem] = {}
        self._dead: Dict[str, DeadLetter] = {}

    def enqueue(self, item: WorkItem) -> WorkItem:
        existing = self._items.get(item.item_id)
        if existing is not None:
            if existing == item:
                return existing
            raise StorageConflictError("work item id already exists")
        if item.item_id in self._dead:
            raise StorageConflictError("work item id already exists in dead letters")
        self._items[item.item_id] = item
        return item

    def claim(self, owner: str, now: datetime, lease: timedelta, limit: int = 1) -> Tuple[WorkItem, ...]:
        if not owner.strip() or lease.total_seconds() <= 0 or limit <= 0:
            raise ValidationError("claim owner, lease, and limit must be valid")
        timestamp = ensure_utc(now, "claim time")
        eligible = [
            item for item in self._items.values()
            if item.available_at <= timestamp and (item.leased_until is None or item.leased_until <= timestamp)
        ]
        eligible.sort(key=lambda item: (item.available_at, item.item_id))
        claimed = []
        for item in eligible[:limit]:
            changed = replace(item, leased_until=timestamp + lease, lease_owner=owner)
            self._items[item.item_id] = changed
            claimed.append(changed)
        return tuple(claimed)

    def complete(self, item_id: str, owner: str) -> WorkItem:
        item = self._owned(item_id, owner)
        return self._items.pop(item_id)

    def fail(self, item_id: str, owner: str, now: datetime, error: str) -> Optional[WorkItem]:
        item = self._owned(item_id, owner)
        timestamp = ensure_utc(now, "failure time")
        attempts = item.attempts + 1
        if attempts >= item.maximum_attempts:
            failed = replace(item, attempts=attempts, leased_until=None, lease_owner=None, last_error=error.strip())
            self._items.pop(item_id)
            self._dead[item_id] = DeadLetter(failed, timestamp, error.strip())
            return None
        delay_seconds = min(
            self.base_delay.total_seconds() * (2 ** (attempts - 1)),
            self.maximum_delay.total_seconds(),
        )
        changed = replace(
            item, attempts=attempts, available_at=timestamp + timedelta(seconds=delay_seconds),
            leased_until=None, lease_owner=None, last_error=error.strip(),
        )
        self._items[item_id] = changed
        return changed

    def _owned(self, item_id: str, owner: str) -> WorkItem:
        try:
            item = self._items[item_id]
        except KeyError as exc:
            raise NotFoundError("work item was not found") from exc
        if item.lease_owner != owner:
            raise StorageConflictError("work item lease belongs to another worker")
        return item

    def dead_letters(self) -> Tuple[DeadLetter, ...]:
        return tuple(sorted(self._dead.values(), key=lambda item: (item.failed_at, item.item.item_id)))

    def redrive(self, item_id: str, now: datetime) -> WorkItem:
        try:
            dead = self._dead.pop(item_id)
        except KeyError as exc:
            raise NotFoundError("dead letter was not found") from exc
        item = replace(dead.item, attempts=0, available_at=ensure_utc(now), leased_until=None, lease_owner=None, last_error=None)
        self._items[item_id] = item
        return item
