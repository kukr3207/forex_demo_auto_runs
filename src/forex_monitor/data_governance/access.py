"""Purpose-aware access decisions, masking, and auditable grants."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from forex_monitor.data_governance.models import AccessPolicy, Classification, Dataset, text, utc
from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError


@dataclass(frozen=True)
class AccessDecision:
    principal: str
    dataset_id: str
    permission: str
    allowed: bool
    policy_id: Optional[str]
    reason: str
    masked_fields: Tuple[str, ...] = ()
    row_filter: Optional[str] = None


@dataclass(frozen=True)
class AccessEvent:
    event_id: str
    principal: str
    dataset_id: str
    permission: str
    purpose: str
    occurred_at: datetime
    allowed: bool
    policy_id: Optional[str]
    record_count: int = 0


class DataAccessService:
    def __init__(self, *, masking_salt: str = "forex-monitor") -> None:
        self._policies: Dict[str, AccessPolicy] = {}
        self._events: Dict[str, AccessEvent] = {}
        self._masking_salt = text(masking_salt, "masking salt").encode("utf-8")
        self._lock = RLock()

    def add_policy(self, policy: AccessPolicy) -> AccessPolicy:
        if not isinstance(policy, AccessPolicy):
            raise ValidationError("policy must be AccessPolicy")
        with self._lock:
            existing = self._policies.get(policy.policy_id)
            if existing is not None:
                if existing == policy:
                    return existing
                raise StorageConflictError("access policy id already exists")
            self._policies[policy.policy_id] = policy
            return policy

    def decide(self, principal: str, dataset: Dataset, permission: str, purpose: str, *, at: datetime) -> AccessDecision:
        identity = text(principal, "principal")
        action = text(permission, "permission").lower()
        use = text(purpose, "purpose")
        instant = utc(at, "access time")
        with self._lock:
            matches = [item for item in self._policies.values() if item.dataset_id == dataset.dataset_id and identity in item.principals and action in item.permissions and item.active and (item.expires_at is None or instant < item.expires_at) and item.purpose == use]
        if not dataset.active:
            return AccessDecision(identity, dataset.dataset_id, action, False, None, "dataset is inactive")
        if not matches:
            return AccessDecision(identity, dataset.dataset_id, action, False, None, "no matching active policy")
        if len(matches) > 1:
            matches.sort(key=lambda item: item.policy_id)
        policy = matches[0]
        return AccessDecision(identity, dataset.dataset_id, action, True, policy.policy_id, "policy allows access", policy.masked_fields, policy.row_filter)

    def record(self, event_id: str, decision: AccessDecision, *, purpose: str, occurred_at: datetime, record_count: int = 0) -> AccessEvent:
        if record_count < 0:
            raise ValidationError("record count cannot be negative")
        event = AccessEvent(text(event_id, "event id"), decision.principal, decision.dataset_id, decision.permission, text(purpose, "purpose"), utc(occurred_at, "occurred at"), decision.allowed, decision.policy_id, record_count)
        with self._lock:
            existing = self._events.get(event.event_id)
            if existing is not None:
                if existing == event:
                    return existing
                raise StorageConflictError("access event id already exists")
            self._events[event.event_id] = event
            return event

    def mask(self, row: Mapping[str, Any], fields: Iterable[str]) -> Dict[str, Any]:
        result = dict(row)
        for field in fields:
            if field not in result or result[field] is None:
                continue
            raw = str(result[field]).encode("utf-8")
            result[field] = "masked:%s" % hashlib.sha256(self._masking_salt + raw).hexdigest()[:16]
        return result

    def events(self, *, principal: Optional[str] = None, dataset_id: Optional[str] = None, allowed: Optional[bool] = None) -> Tuple[AccessEvent, ...]:
        with self._lock:
            values = [item for item in self._events.values() if (principal is None or item.principal == principal) and (dataset_id is None or item.dataset_id == dataset_id) and (allowed is None or item.allowed == allowed)]
            return tuple(sorted(values, key=lambda item: (item.occurred_at, item.event_id)))
