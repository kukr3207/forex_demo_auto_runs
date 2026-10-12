"""Retention schedules, legal-hold exclusions, and deletion manifests."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from threading import RLock
from typing import Dict, Iterable, Mapping, Optional, Tuple

from forex_monitor.data_governance.models import Dataset, text, utc
from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError


class RetentionAction(str, Enum):
    DELETE = "delete"
    ARCHIVE = "archive"
    ANONYMIZE = "anonymize"


@dataclass(frozen=True)
class RetentionPolicy:
    policy_id: str
    dataset_id: str
    action: RetentionAction
    retention_days: int
    timestamp_field: str
    archive_uri: Optional[str] = None
    partition_format: str = "%Y-%m-%d"
    active: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "policy_id", text(self.policy_id, "retention policy id"))
        object.__setattr__(self, "dataset_id", text(self.dataset_id, "dataset id"))
        object.__setattr__(self, "action", RetentionAction(self.action))
        if self.retention_days < 1 or self.retention_days > 36500:
            raise ValidationError("retention days are invalid")
        object.__setattr__(self, "timestamp_field", text(self.timestamp_field, "timestamp field"))
        if self.action == RetentionAction.ARCHIVE and not self.archive_uri:
            raise ValidationError("archive retention requires archive URI")


@dataclass(frozen=True)
class RetentionCandidate:
    record_id: str
    partition: str
    occurred_at: datetime
    subject_id: Optional[str] = None
    size_bytes: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "record_id", text(self.record_id, "record id"))
        object.__setattr__(self, "partition", text(self.partition, "partition"))
        object.__setattr__(self, "occurred_at", utc(self.occurred_at, "occurred at"))
        if self.size_bytes < 0:
            raise ValidationError("candidate size cannot be negative")


@dataclass(frozen=True)
class RetentionPlan:
    plan_id: str
    policy_id: str
    dataset_id: str
    action: RetentionAction
    cutoff: datetime
    candidate_ids: Tuple[str, ...]
    excluded_ids: Tuple[str, ...]
    estimated_bytes: int
    created_at: datetime
    digest: str


@dataclass(frozen=True)
class RetentionExecution:
    execution_id: str
    plan_id: str
    started_at: datetime
    completed_at: datetime
    processed_ids: Tuple[str, ...]
    failed: Tuple[Tuple[str, str], ...]
    manifest_uri: str

    @property
    def success_count(self) -> int:
        return len(self.processed_ids)


class RetentionService:
    def __init__(self) -> None:
        self._policies: Dict[str, RetentionPolicy] = {}
        self._plans: Dict[str, RetentionPlan] = {}
        self._executions: Dict[str, RetentionExecution] = {}
        self._lock = RLock()

    def add_policy(self, policy: RetentionPolicy) -> RetentionPolicy:
        with self._lock:
            existing = self._policies.get(policy.policy_id)
            if existing is not None:
                if existing == policy:
                    return existing
                raise StorageConflictError("retention policy id already exists")
            if any(item.dataset_id == policy.dataset_id and item.active and policy.active for item in self._policies.values()):
                raise StorageConflictError("dataset already has an active retention policy")
            self._policies[policy.policy_id] = policy
            return policy

    def plan(
        self,
        *,
        plan_id: str,
        policy_id: str,
        candidates: Iterable[RetentionCandidate],
        created_at: datetime,
        held_record_ids: Iterable[str] = (),
        held_subject_ids: Iterable[str] = (),
    ) -> RetentionPlan:
        instant = utc(created_at, "created at")
        with self._lock:
            existing = self._plans.get(plan_id)
            if existing is not None:
                return existing
            policy = self._policies.get(policy_id)
            if policy is None or not policy.active:
                raise NotFoundError("active retention policy does not exist")
        cutoff = instant - timedelta(days=policy.retention_days)
        record_holds = set(held_record_ids)
        subject_holds = set(held_subject_ids)
        included = []
        excluded = []
        size = 0
        seen = set()
        for candidate in sorted(candidates, key=lambda item: (item.occurred_at, item.record_id)):
            if candidate.record_id in seen:
                raise ValidationError("retention candidates contain duplicate ids")
            seen.add(candidate.record_id)
            if candidate.occurred_at >= cutoff:
                continue
            if candidate.record_id in record_holds or (candidate.subject_id and candidate.subject_id in subject_holds):
                excluded.append(candidate.record_id)
            else:
                included.append(candidate.record_id)
                size += candidate.size_bytes
        payload = json.dumps({"policy": policy.policy_id, "cutoff": cutoff.isoformat(), "included": included, "excluded": excluded}, sort_keys=True).encode("utf-8")
        plan = RetentionPlan(plan_id, policy.policy_id, policy.dataset_id, policy.action, cutoff, tuple(included), tuple(excluded), size, instant, hashlib.sha256(payload).hexdigest())
        with self._lock:
            self._plans[plan.plan_id] = plan
        return plan

    def complete(
        self,
        *,
        execution_id: str,
        plan_id: str,
        started_at: datetime,
        completed_at: datetime,
        processed_ids: Iterable[str],
        failures: Mapping[str, str],
        manifest_uri: str,
    ) -> RetentionExecution:
        started = utc(started_at, "started at")
        completed = utc(completed_at, "completed at")
        if completed < started:
            raise ValidationError("retention completion precedes start")
        with self._lock:
            existing = self._executions.get(execution_id)
            if existing is not None:
                return existing
            plan = self._plans.get(plan_id)
            if plan is None:
                raise NotFoundError("retention plan does not exist")
            success = tuple(sorted(set(processed_ids)))
            failure_values = tuple(sorted((item, text(reason, "failure reason")) for item, reason in failures.items()))
            handled = set(success) | {item for item, _ in failure_values}
            if handled != set(plan.candidate_ids):
                raise ValidationError("retention execution must account for every candidate")
            execution = RetentionExecution(execution_id, plan_id, started, completed, success, failure_values, text(manifest_uri, "manifest URI"))
            self._executions[execution.execution_id] = execution
            return execution

    def plan_by_id(self, plan_id: str) -> Optional[RetentionPlan]:
        with self._lock:
            return self._plans.get(plan_id.strip())

    def executions(self, plan_id: str) -> Tuple[RetentionExecution, ...]:
        with self._lock:
            values = [item for item in self._executions.values() if item.plan_id == plan_id]
            return tuple(sorted(values, key=lambda item: item.started_at))
