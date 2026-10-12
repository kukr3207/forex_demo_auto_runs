"""Recurring workflow schedules with misfire and overlap handling."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
from threading import RLock
from typing import Dict, Mapping, Optional, Tuple

from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError
from forex_monitor.workflow.engine import WorkflowEngine
from forex_monitor.workflow.models import InstanceState, text, utc


class MisfirePolicy(str, Enum):
    SKIP = "skip"
    RUN_ONCE = "run_once"
    CATCH_UP = "catch_up"


class OverlapPolicy(str, Enum):
    ALLOW = "allow"
    SKIP = "skip"
    QUEUE = "queue"


@dataclass(frozen=True)
class WorkflowSchedule:
    schedule_id: str
    definition_id: str
    interval: timedelta
    next_run_at: datetime
    input: Tuple[Tuple[str, str], ...] = ()
    misfire_policy: MisfirePolicy = MisfirePolicy.RUN_ONCE
    overlap_policy: OverlapPolicy = OverlapPolicy.SKIP
    maximum_catch_up: int = 10
    enabled: bool = True
    last_run_at: Optional[datetime] = None
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "schedule_id", text(self.schedule_id, "schedule id"))
        object.__setattr__(self, "definition_id", text(self.definition_id, "definition id"))
        if self.interval <= timedelta(0):
            raise ValidationError("schedule interval must be positive")
        object.__setattr__(self, "next_run_at", utc(self.next_run_at, "next run at"))
        object.__setattr__(self, "misfire_policy", MisfirePolicy(self.misfire_policy))
        object.__setattr__(self, "overlap_policy", OverlapPolicy(self.overlap_policy))
        if self.maximum_catch_up < 1 or self.maximum_catch_up > 1000:
            raise ValidationError("maximum catch-up runs are invalid")
        if self.last_run_at is not None:
            object.__setattr__(self, "last_run_at", utc(self.last_run_at, "last run at"))


class WorkflowScheduler:
    def __init__(self, engine: WorkflowEngine) -> None:
        self.engine = engine
        self._schedules: Dict[str, WorkflowSchedule] = {}
        self._runs: Dict[str, list[str]] = {}
        self._lock = RLock()

    def add(self, schedule: WorkflowSchedule) -> WorkflowSchedule:
        with self._lock:
            existing = self._schedules.get(schedule.schedule_id)
            if existing is not None:
                if existing == schedule:
                    return existing
                raise StorageConflictError("workflow schedule id already exists")
            self._schedules[schedule.schedule_id] = schedule
            self._runs[schedule.schedule_id] = []
            return schedule

    def due(self, at: datetime) -> Tuple[WorkflowSchedule, ...]:
        instant = utc(at, "scheduler time")
        with self._lock:
            values = [item for item in self._schedules.values() if item.enabled and item.next_run_at <= instant]
            return tuple(sorted(values, key=lambda item: (item.next_run_at, item.schedule_id)))

    def dispatch(self, at: datetime) -> Tuple[str, ...]:
        instant = utc(at, "scheduler time")
        created = []
        with self._lock:
            schedules = self.due(instant)
            for schedule in schedules:
                active = [instance_id for instance_id in self._runs[schedule.schedule_id] if self.engine.instance(instance_id).state not in {InstanceState.SUCCEEDED, InstanceState.FAILED, InstanceState.CANCELLED, InstanceState.COMPENSATED}]
                if active and schedule.overlap_policy == OverlapPolicy.SKIP:
                    self._advance_schedule(schedule, instant, skipped=True)
                    continue
                if active and schedule.overlap_policy == OverlapPolicy.QUEUE:
                    continue
                run_times = self._run_times(schedule, instant)
                for run_time in run_times:
                    instance_id = "%s:%s" % (schedule.schedule_id, run_time.strftime("%Y%m%dT%H%M%S%fZ"))
                    instance = self.engine.start(instance_id=instance_id, definition_id=schedule.definition_id, correlation_id=instance_id, input=dict(schedule.input), started_at=instant)
                    self._runs[schedule.schedule_id].append(instance.instance_id)
                    created.append(instance.instance_id)
                self._advance_schedule(schedule, instant, skipped=False)
        return tuple(created)

    def pause(self, schedule_id: str, *, expected_version: int) -> WorkflowSchedule:
        with self._lock:
            current = self._require(schedule_id)
            if current.version != expected_version:
                raise StorageConflictError("workflow schedule version does not match")
            changed = replace(current, enabled=False, version=current.version + 1)
            self._schedules[current.schedule_id] = changed
            return changed

    def resume(self, schedule_id: str, *, next_run_at: datetime, expected_version: int) -> WorkflowSchedule:
        with self._lock:
            current = self._require(schedule_id)
            if current.version != expected_version:
                raise StorageConflictError("workflow schedule version does not match")
            changed = replace(current, enabled=True, next_run_at=utc(next_run_at, "next run at"), version=current.version + 1)
            self._schedules[current.schedule_id] = changed
            return changed

    def runs(self, schedule_id: str) -> Tuple[str, ...]:
        with self._lock:
            return tuple(self._runs.get(schedule_id, ()))

    def _run_times(self, schedule: WorkflowSchedule, at: datetime) -> Tuple[datetime, ...]:
        if schedule.next_run_at > at:
            return ()
        if schedule.misfire_policy == MisfirePolicy.SKIP:
            return ()
        if schedule.misfire_policy == MisfirePolicy.RUN_ONCE:
            return (schedule.next_run_at,)
        result = []
        current = schedule.next_run_at
        while current <= at and len(result) < schedule.maximum_catch_up:
            result.append(current)
            current += schedule.interval
        return tuple(result)

    def _advance_schedule(self, schedule: WorkflowSchedule, at: datetime, *, skipped: bool) -> None:
        next_run = schedule.next_run_at
        while next_run <= at:
            next_run += schedule.interval
        changed = replace(schedule, next_run_at=next_run, last_run_at=None if skipped else at, version=schedule.version + 1)
        self._schedules[schedule.schedule_id] = changed

    def _require(self, schedule_id: str) -> WorkflowSchedule:
        value = self._schedules.get(text(schedule_id, "schedule id"))
        if value is None:
            raise NotFoundError("workflow schedule does not exist")
        return value
