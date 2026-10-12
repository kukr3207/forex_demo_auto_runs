"""Resumable operational runbooks with evidence and approval gates."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Dict, Mapping, Optional, Sequence, Tuple

from forex_monitor.errors import StorageConflictError, ValidationError
from forex_monitor.models import ensure_utc


class StepState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class RunbookStep:
    step_id: str
    title: str
    instruction: str
    requires_approval: bool = False
    optional: bool = False

    def __post_init__(self) -> None:
        if not self.step_id.strip() or not self.title.strip() or not self.instruction.strip():
            raise ValidationError("runbook step fields cannot be blank")


@dataclass(frozen=True)
class Runbook:
    runbook_id: str
    title: str
    service: str
    steps: Tuple[RunbookStep, ...]

    def __post_init__(self) -> None:
        if not self.runbook_id.strip() or not self.title.strip() or not self.service.strip():
            raise ValidationError("runbook fields cannot be blank")
        if not self.steps:
            raise ValidationError("runbook requires at least one step")
        if len({step.step_id for step in self.steps}) != len(self.steps):
            raise ValidationError("runbook step ids must be unique")


@dataclass(frozen=True)
class StepExecution:
    step_id: str
    state: StepState = StepState.PENDING
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    operator: Optional[str] = None
    evidence: Optional[str] = None
    approved_by: Optional[str] = None


@dataclass(frozen=True)
class RunbookExecution:
    execution_id: str
    runbook_id: str
    started_at: datetime
    operator: str
    steps: Tuple[StepExecution, ...]
    incident_id: Optional[str] = None

    @property
    def complete(self) -> bool:
        return all(step.state in {StepState.SUCCEEDED, StepState.SKIPPED} for step in self.steps)

    @property
    def failed(self) -> bool:
        return any(step.state is StepState.FAILED for step in self.steps)

    @property
    def next_step(self) -> Optional[StepExecution]:
        return next((step for step in self.steps if step.state is StepState.PENDING), None)


class RunbookManager:
    def __init__(self) -> None:
        self._runbooks: Dict[str, Runbook] = {}
        self._executions: Dict[str, RunbookExecution] = {}

    def register(self, runbook: Runbook) -> None:
        if runbook.runbook_id in self._runbooks:
            raise StorageConflictError("runbook id already exists")
        self._runbooks[runbook.runbook_id] = runbook

    def start(self, execution_id: str, runbook_id: str, operator: str, at: datetime, incident_id: Optional[str] = None) -> RunbookExecution:
        if execution_id in self._executions:
            raise StorageConflictError("runbook execution id already exists")
        try:
            runbook = self._runbooks[runbook_id]
        except KeyError as exc:
            raise ValidationError("runbook was not found") from exc
        execution = RunbookExecution(
            execution_id, runbook_id, ensure_utc(at, "runbook start"), operator.strip(),
            tuple(StepExecution(step.step_id) for step in runbook.steps), incident_id,
        )
        self._executions[execution_id] = execution
        return execution

    def complete_step(
        self,
        execution_id: str,
        step_id: str,
        operator: str,
        started_at: datetime,
        finished_at: datetime,
        *,
        succeeded: bool,
        evidence: str,
        approved_by: Optional[str] = None,
    ) -> RunbookExecution:
        execution = self._executions[execution_id]
        if execution.complete or execution.failed:
            raise ValidationError("runbook execution is already terminal")
        expected = execution.next_step
        if expected is None or expected.step_id != step_id:
            raise ValidationError("runbook steps must execute in order")
        runbook = self._runbooks[execution.runbook_id]
        definition = next(item for item in runbook.steps if item.step_id == step_id)
        if definition.requires_approval and not approved_by:
            raise ValidationError("runbook step requires approval")
        start = ensure_utc(started_at, "step start")
        finish = ensure_utc(finished_at, "step finish")
        if finish < start:
            raise ValidationError("runbook step finish cannot precede start")
        changed_step = StepExecution(
            step_id, StepState.SUCCEEDED if succeeded else StepState.FAILED,
            start, finish, operator.strip(), evidence.strip(), approved_by,
        )
        steps = tuple(changed_step if item.step_id == step_id else item for item in execution.steps)
        changed = replace(execution, steps=steps)
        self._executions[execution_id] = changed
        return changed
