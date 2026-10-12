"""Validated workflow definitions and immutable execution events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional, Tuple

from forex_monitor.errors import ValidationError


def text(value: object, name: str, maximum: Optional[int] = None) -> str:
    result = str(value).strip() if value is not None else ""
    if not result:
        raise ValidationError("%s is required" % name)
    if maximum is not None and len(result) > maximum:
        raise ValidationError("%s exceeds maximum length" % name)
    return result


def utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError("%s must be timezone-aware" % name)
    return value.astimezone(timezone.utc)


class StepType(str, Enum):
    TASK = "task"
    APPROVAL = "approval"
    TIMER = "timer"
    SIGNAL = "signal"
    PARALLEL = "parallel"


class InstanceState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    COMPENSATING = "compensating"
    COMPENSATED = "compensated"


class StepState(str, Enum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    WAITING = "waiting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    COMPENSATED = "compensated"


class EventType(str, Enum):
    STARTED = "started"
    STEP_STARTED = "step_started"
    STEP_SUCCEEDED = "step_succeeded"
    STEP_FAILED = "step_failed"
    RETRY_SCHEDULED = "retry_scheduled"
    SIGNAL_RECEIVED = "signal_received"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    COMPENSATION_STARTED = "compensation_started"
    STEP_COMPENSATED = "step_compensated"
    COMPLETED = "completed"


@dataclass(frozen=True)
class RetryPolicy:
    maximum_attempts: int = 1
    initial_delay: timedelta = timedelta(0)
    multiplier: float = 2.0
    maximum_delay: timedelta = timedelta(hours=1)
    retryable_errors: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.maximum_attempts < 1 or self.maximum_attempts > 100:
            raise ValidationError("maximum attempts are invalid")
        if self.initial_delay < timedelta(0) or self.maximum_delay < self.initial_delay:
            raise ValidationError("retry delays are invalid")
        if self.multiplier < 1:
            raise ValidationError("retry multiplier must be at least one")
        object.__setattr__(self, "retryable_errors", tuple(sorted(set(self.retryable_errors))))

    def delay_for(self, attempt: int) -> timedelta:
        if attempt < 1:
            raise ValidationError("attempt must be positive")
        seconds = self.initial_delay.total_seconds() * (self.multiplier ** (attempt - 1))
        return min(timedelta(seconds=seconds), self.maximum_delay)


@dataclass(frozen=True)
class WorkflowStep:
    step_id: str
    name: str
    step_type: StepType
    handler: str
    dependencies: Tuple[str, ...] = ()
    retry_policy: RetryPolicy = RetryPolicy()
    timeout: Optional[timedelta] = None
    compensation_handler: Optional[str] = None
    approval_roles: Tuple[str, ...] = ()
    required_approvals: int = 1
    signal_name: Optional[str] = None
    timer_delay: Optional[timedelta] = None
    condition: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "step_id", text(self.step_id, "step id"))
        object.__setattr__(self, "name", text(self.name, "step name", 200))
        object.__setattr__(self, "step_type", StepType(self.step_type))
        object.__setattr__(self, "handler", text(self.handler, "step handler"))
        dependencies = tuple(sorted(set(self.dependencies)))
        if self.step_id in dependencies:
            raise ValidationError("workflow step cannot depend on itself")
        object.__setattr__(self, "dependencies", dependencies)
        if self.timeout is not None and self.timeout <= timedelta(0):
            raise ValidationError("step timeout must be positive")
        roles = tuple(sorted(set(item.strip().lower() for item in self.approval_roles if item.strip())))
        object.__setattr__(self, "approval_roles", roles)
        if self.step_type == StepType.APPROVAL:
            if not roles or self.required_approvals < 1 or self.required_approvals > len(roles):
                raise ValidationError("approval step roles or quorum are invalid")
        if self.step_type == StepType.SIGNAL and not self.signal_name:
            raise ValidationError("signal step requires signal name")
        if self.step_type == StepType.TIMER and (self.timer_delay is None or self.timer_delay < timedelta(0)):
            raise ValidationError("timer step requires non-negative delay")


@dataclass(frozen=True)
class WorkflowDefinition:
    definition_id: str
    version: int
    name: str
    steps: Tuple[WorkflowStep, ...]
    created_at: datetime
    created_by: str
    active: bool = True
    tags: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "definition_id", text(self.definition_id, "definition id"))
        if self.version < 1:
            raise ValidationError("definition version must be positive")
        object.__setattr__(self, "name", text(self.name, "workflow name", 200))
        ids = [item.step_id for item in self.steps]
        if not ids or len(ids) != len(set(ids)):
            raise ValidationError("workflow steps are empty or duplicated")
        known = set(ids)
        if any(set(item.dependencies) - known for item in self.steps):
            raise ValidationError("workflow step has unknown dependency")
        self._check_cycles()
        object.__setattr__(self, "created_at", utc(self.created_at, "created at"))
        object.__setattr__(self, "created_by", text(self.created_by, "definition creator"))
        object.__setattr__(self, "tags", tuple(sorted(set(self.tags))))

    def _check_cycles(self) -> None:
        dependencies = {item.step_id: set(item.dependencies) for item in self.steps}
        resolved = set()
        while dependencies:
            ready = {key for key, values in dependencies.items() if values <= resolved}
            if not ready:
                raise ValidationError("workflow definition contains a cycle")
            resolved.update(ready)
            for key in ready:
                dependencies.pop(key)

    @property
    def key(self) -> Tuple[str, int]:
        return self.definition_id, self.version


@dataclass(frozen=True)
class StepExecution:
    step_id: str
    state: StepState = StepState.PENDING
    attempt: int = 0
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    next_attempt_at: Optional[datetime] = None
    output: Tuple[Tuple[str, str], ...] = ()
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    approvals: Tuple[Tuple[str, str], ...] = ()
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "step_id", text(self.step_id, "step id"))
        object.__setattr__(self, "state", StepState(self.state))
        if self.attempt < 0:
            raise ValidationError("step attempt cannot be negative")
        if self.started_at is not None:
            object.__setattr__(self, "started_at", utc(self.started_at, "started at"))
        if self.completed_at is not None:
            object.__setattr__(self, "completed_at", utc(self.completed_at, "completed at"))
        if self.next_attempt_at is not None:
            object.__setattr__(self, "next_attempt_at", utc(self.next_attempt_at, "next attempt at"))


@dataclass(frozen=True)
class WorkflowInstance:
    instance_id: str
    definition_id: str
    definition_version: int
    state: InstanceState
    created_at: datetime
    correlation_id: str
    input: Tuple[Tuple[str, str], ...]
    steps: Tuple[StepExecution, ...]
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    cancellation_reason: Optional[str] = None
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "instance_id", text(self.instance_id, "instance id"))
        object.__setattr__(self, "definition_id", text(self.definition_id, "definition id"))
        object.__setattr__(self, "state", InstanceState(self.state))
        object.__setattr__(self, "created_at", utc(self.created_at, "created at"))
        object.__setattr__(self, "correlation_id", text(self.correlation_id, "correlation id"))
        ids = [item.step_id for item in self.steps]
        if len(ids) != len(set(ids)):
            raise ValidationError("instance step executions are duplicated")


@dataclass(frozen=True)
class WorkflowEvent:
    event_id: str
    instance_id: str
    event_type: EventType
    occurred_at: datetime
    step_id: Optional[str] = None
    actor: str = "system"
    details: Tuple[Tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", text(self.event_id, "event id"))
        object.__setattr__(self, "instance_id", text(self.instance_id, "instance id"))
        object.__setattr__(self, "event_type", EventType(self.event_type))
        object.__setattr__(self, "occurred_at", utc(self.occurred_at, "occurred at"))
        object.__setattr__(self, "actor", text(self.actor, "actor"))
