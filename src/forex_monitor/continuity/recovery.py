"""Restore-plan validation, execution, verification, and recovery drills."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from threading import RLock
from typing import Dict, Iterable, Mapping, Optional, Tuple

from forex_monitor.continuity.backups import BackupCatalog
from forex_monitor.continuity.models import BackupState, RecoveryDrill, RecoveryObjective, RecoveryState, RestorePlan, text, utc
from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError


class RecoveryPlanner:
    def __init__(self, backups: BackupCatalog) -> None:
        self.backups = backups
        self._objectives: Dict[str, RecoveryObjective] = {}
        self._plans: Dict[str, RestorePlan] = {}
        self._drills: Dict[str, RecoveryDrill] = {}
        self._lock = RLock()

    def set_objective(self, objective: RecoveryObjective) -> RecoveryObjective:
        with self._lock:
            self._objectives[objective.resource_id] = objective
            return objective

    def plan(self, *, plan_id: str, resource_id: str, target_time: datetime, target_environment: str, created_at: datetime, created_by: str) -> RestorePlan:
        target = utc(target_time, "target time")
        chain = self.backups.restore_chain(resource_id, target)
        value = RestorePlan(plan_id, resource_id, target, tuple(item.backup_id for item in chain), target_environment, created_at, created_by, sum(item.size_bytes for item in chain))
        with self._lock:
            existing = self._plans.get(value.plan_id)
            if existing is not None:
                if existing == value:
                    return existing
                raise StorageConflictError("restore plan id already exists")
            self._plans[value.plan_id] = value
            return value

    def validate(self, plan_id: str, *, expected_version: int) -> RestorePlan:
        with self._lock:
            current = self._require_plan(plan_id)
            self._check_version(current, expected_version)
            if current.state != RecoveryState.PLANNED:
                raise ValidationError("only planned restores can be validated")
            errors = []
            prior = None
            for backup_id in current.backup_ids:
                backup = self.backups.require(backup_id)
                if backup.state != BackupState.AVAILABLE:
                    errors.append("backup %s is not available" % backup_id)
                if backup.resource_id != current.resource_id:
                    errors.append("backup %s belongs to another resource" % backup_id)
                if prior is not None and backup.base_backup_id != prior.backup_id:
                    errors.append("backup %s does not continue restore chain" % backup_id)
                prior = backup
            changed = replace(current, state=RecoveryState.PLANNED if errors else RecoveryState.VALIDATING, validation_errors=tuple(errors), version=current.version + 1)
            self._plans[current.plan_id] = changed
            return changed

    def start(self, plan_id: str, *, at: datetime, expected_version: int) -> RestorePlan:
        instant = utc(at, "restore start")
        with self._lock:
            current = self._require_plan(plan_id)
            self._check_version(current, expected_version)
            if current.state != RecoveryState.VALIDATING or current.validation_errors:
                raise ValidationError("restore plan has not passed validation")
            changed = replace(current, state=RecoveryState.RESTORING, started_at=instant, version=current.version + 1)
            self._plans[current.plan_id] = changed
            return changed

    def begin_verification(self, plan_id: str, *, expected_version: int) -> RestorePlan:
        with self._lock:
            current = self._require_plan(plan_id)
            self._check_version(current, expected_version)
            if current.state != RecoveryState.RESTORING:
                raise ValidationError("restore is not running")
            changed = replace(current, state=RecoveryState.VERIFYING, version=current.version + 1)
            self._plans[current.plan_id] = changed
            return changed

    def complete(self, plan_id: str, *, checks: Mapping[str, bool], at: datetime, expected_version: int) -> RestorePlan:
        instant = utc(at, "restore completion")
        if not checks:
            raise ValidationError("restore completion requires verification checks")
        with self._lock:
            current = self._require_plan(plan_id)
            self._check_version(current, expected_version)
            if current.state != RecoveryState.VERIFYING:
                raise ValidationError("restore is not being verified")
            failures = tuple(sorted(name for name, passed in checks.items() if not passed))
            changed = replace(current, state=RecoveryState.FAILED if failures else RecoveryState.COMPLETED, completed_at=instant, failure_reason=", ".join(failures) if failures else None, version=current.version + 1)
            self._plans[current.plan_id] = changed
            return changed

    def record_drill(self, drill: RecoveryDrill) -> Tuple[RecoveryDrill, bool]:
        with self._lock:
            existing = self._drills.get(drill.drill_id)
            if existing is not None:
                if existing == drill:
                    return existing, self.meets_objective(existing)
                raise StorageConflictError("recovery drill id already exists")
            self._drills[drill.drill_id] = drill
            return drill, self.meets_objective(drill)

    def meets_objective(self, drill: RecoveryDrill) -> bool:
        objective = self._objectives.get(drill.resource_id)
        if objective is None:
            raise NotFoundError("resource recovery objective does not exist")
        return drill.passed and drill.data_loss_seconds <= objective.recovery_point.total_seconds() and drill.recovery_seconds <= objective.recovery_time.total_seconds()

    def drills(self, resource_id: str) -> Tuple[RecoveryDrill, ...]:
        with self._lock:
            values = [item for item in self._drills.values() if item.resource_id == resource_id]
            return tuple(sorted(values, key=lambda item: item.started_at))

    def _require_plan(self, plan_id: str) -> RestorePlan:
        value = self._plans.get(text(plan_id, "plan id"))
        if value is None:
            raise NotFoundError("restore plan does not exist")
        return value

    @staticmethod
    def _check_version(plan: RestorePlan, expected_version: int) -> None:
        if plan.version != expected_version:
            raise StorageConflictError("restore plan version does not match")
