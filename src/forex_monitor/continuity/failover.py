"""Region health and controlled failover or failback transitions."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from threading import RLock
from typing import Dict, Iterable, Mapping, Optional, Tuple

from forex_monitor.continuity.models import FailoverGroup, FailoverState, RegionStatus, text, utc
from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError


class FailoverCoordinator:
    def __init__(self) -> None:
        self._groups: Dict[str, FailoverGroup] = {}
        self._status: Dict[Tuple[str, str], RegionStatus] = {}
        self._approvals: Dict[str, Tuple[str, ...]] = {}
        self._lock = RLock()

    def add_group(self, group: FailoverGroup) -> FailoverGroup:
        with self._lock:
            existing = self._groups.get(group.group_id)
            if existing is not None:
                if existing == group:
                    return existing
                raise StorageConflictError("failover group id already exists")
            self._groups[group.group_id] = group
            self._approvals[group.group_id] = ()
            return group

    def observe(self, group_id: str, status: RegionStatus) -> FailoverGroup:
        with self._lock:
            current = self._require(group_id)
            if status.region not in (current.primary_region,) + current.secondary_regions:
                raise ValidationError("region does not belong to failover group")
            key = (group_id, status.region)
            prior = self._status.get(key)
            if prior and status.observed_at < prior.observed_at:
                raise StorageConflictError("region health observation moved backward")
            self._status[key] = status
            active = self._status.get((group_id, current.active_region))
            new_state = current.state
            if active and not active.healthy and current.state == FailoverState.HEALTHY:
                new_state = FailoverState.FAILOVER_PENDING if not current.automatic else FailoverState.DEGRADED
            elif active and active.healthy and current.state == FailoverState.DEGRADED:
                new_state = FailoverState.HEALTHY
            if new_state != current.state:
                current = replace(current, state=new_state, version=current.version + 1)
                self._groups[group_id] = current
            return current

    def candidates(self, group_id: str, *, at: datetime, maximum_age: timedelta = timedelta(minutes=1)) -> Tuple[RegionStatus, ...]:
        instant = utc(at, "candidate time")
        with self._lock:
            group = self._require(group_id)
            values = [self._status.get((group_id, region)) for region in group.secondary_regions]
            values = [item for item in values if item is not None and item.healthy and item.writable and item.lag_seconds <= group.maximum_lag_seconds and instant - item.observed_at <= maximum_age]
            return tuple(sorted(values, key=lambda item: (item.lag_seconds, item.region)))

    def approve(self, group_id: str, approver: str) -> FailoverGroup:
        identity = text(approver, "approver")
        with self._lock:
            current = self._require(group_id)
            if current.state not in {FailoverState.FAILOVER_PENDING, FailoverState.FAILBACK_PENDING}:
                raise ValidationError("failover group is not awaiting approval")
            self._approvals[group_id] = tuple(sorted(set(self._approvals[group_id] + (identity,))))
            return current

    def failover(self, group_id: str, *, target_region: str, expected_version: int, required_approvals: int = 2) -> FailoverGroup:
        with self._lock:
            current = self._require(group_id)
            if current.version != expected_version:
                raise StorageConflictError("failover group version does not match")
            if current.state not in {FailoverState.FAILOVER_PENDING, FailoverState.DEGRADED}:
                raise ValidationError("failover group is not ready to fail over")
            if not current.automatic and len(self._approvals[group_id]) < required_approvals:
                raise ValidationError("failover lacks required approvals")
            candidate = self._status.get((group_id, target_region))
            if candidate is None or not candidate.healthy or not candidate.writable or candidate.lag_seconds > current.maximum_lag_seconds:
                raise ValidationError("target region is not a valid failover candidate")
            changed = replace(current, active_region=target_region, state=FailoverState.FAILED_OVER, version=current.version + 1)
            self._groups[group_id] = changed
            self._approvals[group_id] = ()
            return changed

    def request_failback(self, group_id: str, *, expected_version: int) -> FailoverGroup:
        with self._lock:
            current = self._require(group_id)
            if current.version != expected_version:
                raise StorageConflictError("failover group version does not match")
            if current.state != FailoverState.FAILED_OVER:
                raise ValidationError("group is not failed over")
            primary = self._status.get((group_id, current.primary_region))
            if primary is None or not primary.healthy or not primary.writable or primary.lag_seconds > current.maximum_lag_seconds:
                raise ValidationError("primary region is not ready for failback")
            changed = replace(current, state=FailoverState.FAILBACK_PENDING, version=current.version + 1)
            self._groups[group_id] = changed
            return changed

    def failback(self, group_id: str, *, expected_version: int, required_approvals: int = 2) -> FailoverGroup:
        with self._lock:
            current = self._require(group_id)
            if current.version != expected_version:
                raise StorageConflictError("failover group version does not match")
            if current.state != FailoverState.FAILBACK_PENDING or len(self._approvals[group_id]) < required_approvals:
                raise ValidationError("failback is not approved")
            changed = replace(current, active_region=current.primary_region, state=FailoverState.HEALTHY, version=current.version + 1)
            self._groups[group_id] = changed
            self._approvals[group_id] = ()
            return changed

    def _require(self, group_id: str) -> FailoverGroup:
        value = self._groups.get(text(group_id, "group id"))
        if value is None:
            raise NotFoundError("failover group does not exist")
        return value
