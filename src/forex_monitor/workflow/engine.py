"""Deterministic event-recorded workflow state machine."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from threading import RLock
from typing import Dict, Iterable, Mapping, Optional, Tuple

from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError
from forex_monitor.workflow.models import EventType, InstanceState, StepExecution, StepState, StepType, WorkflowDefinition, WorkflowEvent, WorkflowInstance, text, utc


class WorkflowEngine:
    def __init__(self) -> None:
        self._definitions: Dict[Tuple[str, int], WorkflowDefinition] = {}
        self._active_versions: Dict[str, int] = {}
        self._instances: Dict[str, WorkflowInstance] = {}
        self._correlations: Dict[Tuple[str, str], str] = {}
        self._events: Dict[str, WorkflowEvent] = {}
        self._instance_events: Dict[str, list[str]] = {}
        self._signals: Dict[Tuple[str, str, str], Tuple[Tuple[str, str], ...]] = {}
        self._lock = RLock()

    def register(self, definition: WorkflowDefinition) -> WorkflowDefinition:
        with self._lock:
            existing = self._definitions.get(definition.key)
            if existing is not None:
                if existing == definition:
                    return existing
                raise StorageConflictError("workflow definition version already exists")
            versions = [version for definition_id, version in self._definitions if definition_id == definition.definition_id]
            if versions and definition.version != max(versions) + 1:
                raise ValidationError("workflow definition versions must be consecutive")
            if not versions and definition.version != 1:
                raise ValidationError("first workflow definition version must be one")
            self._definitions[definition.key] = definition
            if definition.active:
                self._active_versions[definition.definition_id] = definition.version
            return definition

    def start(
        self,
        *,
        instance_id: str,
        definition_id: str,
        correlation_id: str,
        input: Mapping[str, object],
        started_at: datetime,
        version: Optional[int] = None,
    ) -> WorkflowInstance:
        instant = utc(started_at, "started at")
        definition_version = version if version is not None else self._active_versions.get(definition_id)
        if definition_version is None:
            raise NotFoundError("workflow has no active definition")
        with self._lock:
            definition = self._definitions.get((definition_id, definition_version))
            if definition is None or not definition.active:
                raise NotFoundError("active workflow definition does not exist")
            correlation_key = (definition_id, text(correlation_id, "correlation id"))
            existing_id = self._correlations.get(correlation_key)
            if existing_id is not None:
                return self._instances[existing_id]
            if instance_id in self._instances:
                raise StorageConflictError("workflow instance id already exists")
            roots = {item.step_id for item in definition.steps if not item.dependencies}
            executions = tuple(StepExecution(item.step_id, StepState.READY if item.step_id in roots else StepState.PENDING) for item in definition.steps)
            instance = WorkflowInstance(instance_id, definition_id, definition_version, InstanceState.RUNNING, instant, correlation_id, tuple(sorted((key, str(value)) for key, value in input.items())), executions, started_at=instant)
            self._instances[instance.instance_id] = instance
            self._correlations[correlation_key] = instance.instance_id
            self._instance_events[instance.instance_id] = []
            self._append_event(instance.instance_id, EventType.STARTED, instant)
            return instance

    def claim_step(self, instance_id: str, step_id: str, *, at: datetime, expected_version: int) -> StepExecution:
        instant = utc(at, "claim time")
        with self._lock:
            instance = self._require_instance(instance_id)
            self._check_instance_version(instance, expected_version)
            definition = self._definition(instance)
            step = self._step_definition(definition, step_id)
            execution = self._execution(instance, step_id)
            if execution.state != StepState.READY:
                raise ValidationError("workflow step is not ready")
            if step.step_type in {StepType.APPROVAL, StepType.SIGNAL, StepType.TIMER}:
                raise ValidationError("waiting step cannot be claimed as a task")
            changed = replace(execution, state=StepState.RUNNING, attempt=execution.attempt + 1, started_at=instant, next_attempt_at=None, version=execution.version + 1)
            self._store_step(instance, changed)
            self._append_event(instance_id, EventType.STEP_STARTED, instant, step_id=step_id)
            return changed

    def succeed_step(self, instance_id: str, step_id: str, *, output: Mapping[str, object], at: datetime, expected_version: int) -> WorkflowInstance:
        instant = utc(at, "completion time")
        with self._lock:
            instance = self._require_instance(instance_id)
            self._check_instance_version(instance, expected_version)
            execution = self._execution(instance, step_id)
            if execution.state != StepState.RUNNING:
                raise ValidationError("only running workflow steps can succeed")
            changed = replace(execution, state=StepState.SUCCEEDED, completed_at=instant, output=tuple(sorted((key, str(value)) for key, value in output.items())), version=execution.version + 1)
            instance = self._store_step(instance, changed)
            self._append_event(instance_id, EventType.STEP_SUCCEEDED, instant, step_id=step_id)
            return self._advance(instance, instant)

    def fail_step(self, instance_id: str, step_id: str, *, error_code: str, error_message: str, at: datetime, expected_version: int) -> WorkflowInstance:
        instant = utc(at, "failure time")
        with self._lock:
            instance = self._require_instance(instance_id)
            self._check_instance_version(instance, expected_version)
            definition = self._definition(instance)
            step = self._step_definition(definition, step_id)
            execution = self._execution(instance, step_id)
            if execution.state != StepState.RUNNING:
                raise ValidationError("only running workflow steps can fail")
            code = text(error_code, "error code")
            retryable = not step.retry_policy.retryable_errors or code in step.retry_policy.retryable_errors
            if retryable and execution.attempt < step.retry_policy.maximum_attempts:
                next_at = instant + step.retry_policy.delay_for(execution.attempt)
                changed = replace(execution, state=StepState.WAITING, next_attempt_at=next_at, error_code=code, error_message=text(error_message, "error message"), version=execution.version + 1)
                event_type = EventType.RETRY_SCHEDULED
                state = InstanceState.WAITING
            else:
                changed = replace(execution, state=StepState.FAILED, completed_at=instant, error_code=code, error_message=text(error_message, "error message"), version=execution.version + 1)
                event_type = EventType.STEP_FAILED
                state = InstanceState.FAILED
            instance = self._store_step(instance, changed)
            instance = replace(instance, state=state, version=instance.version + 1)
            self._instances[instance.instance_id] = instance
            self._append_event(instance_id, event_type, instant, step_id=step_id, details=(("errorCode", code),))
            return instance

    def tick(self, instance_id: str, *, at: datetime) -> WorkflowInstance:
        instant = utc(at, "tick time")
        with self._lock:
            instance = self._require_instance(instance_id)
            definition = self._definition(instance)
            executions = {item.step_id: item for item in instance.steps}
            changed = False
            for step in definition.steps:
                execution = executions[step.step_id]
                if execution.state == StepState.WAITING and execution.next_attempt_at is not None and execution.next_attempt_at <= instant:
                    executions[step.step_id] = replace(execution, state=StepState.READY, next_attempt_at=None, version=execution.version + 1)
                    changed = True
                elif step.step_type == StepType.TIMER and execution.state == StepState.READY:
                    due = instance.started_at + step.timer_delay if instance.started_at and step.timer_delay else instant
                    if due <= instant:
                        executions[step.step_id] = replace(execution, state=StepState.SUCCEEDED, started_at=due, completed_at=instant, version=execution.version + 1)
                        changed = True
            if changed:
                instance = replace(instance, steps=tuple(executions[item.step_id] for item in definition.steps), state=InstanceState.RUNNING, version=instance.version + 1)
                self._instances[instance.instance_id] = instance
                return self._advance(instance, instant)
            return instance

    def signal(self, instance_id: str, signal_name: str, *, signal_id: str, payload: Mapping[str, object], at: datetime) -> WorkflowInstance:
        instant = utc(at, "signal time")
        key = (instance_id, signal_name, signal_id)
        value = tuple(sorted((name, str(item)) for name, item in payload.items()))
        with self._lock:
            existing = self._signals.get(key)
            if existing is not None:
                if existing == value:
                    return self._require_instance(instance_id)
                raise StorageConflictError("signal id already has different payload")
            instance = self._require_instance(instance_id)
            definition = self._definition(instance)
            matches = [step for step in definition.steps if step.step_type == StepType.SIGNAL and step.signal_name == signal_name and self._execution(instance, step.step_id).state == StepState.READY]
            if len(matches) != 1:
                raise ValidationError("signal does not match exactly one ready step")
            step = matches[0]
            execution = self._execution(instance, step.step_id)
            changed = replace(execution, state=StepState.SUCCEEDED, started_at=instant, completed_at=instant, output=value, version=execution.version + 1)
            self._signals[key] = value
            instance = self._store_step(instance, changed)
            self._append_event(instance_id, EventType.SIGNAL_RECEIVED, instant, step_id=step.step_id, details=(("signal", signal_name),))
            return self._advance(instance, instant)

    def approve(self, instance_id: str, step_id: str, *, approver: str, role: str, approved: bool, at: datetime) -> WorkflowInstance:
        instant = utc(at, "approval time")
        with self._lock:
            instance = self._require_instance(instance_id)
            definition = self._definition(instance)
            step = self._step_definition(definition, step_id)
            execution = self._execution(instance, step_id)
            if step.step_type != StepType.APPROVAL or execution.state != StepState.READY:
                raise ValidationError("workflow step is not awaiting approval")
            role_value = text(role, "approval role").lower()
            if role_value not in step.approval_roles:
                raise ValidationError("approver role is not permitted")
            identity = text(approver, "approver")
            approvals = dict(execution.approvals)
            if identity in approvals:
                if approvals[identity] == ("approve" if approved else "reject"):
                    return instance
                raise StorageConflictError("approver already supplied a different decision")
            approvals[identity] = "approve" if approved else "reject"
            if not approved:
                changed = replace(execution, state=StepState.FAILED, approvals=tuple(sorted(approvals.items())), completed_at=instant, error_code="approval_rejected", error_message="Approval rejected by %s" % identity, version=execution.version + 1)
                instance = self._store_step(instance, changed)
                instance = replace(instance, state=InstanceState.FAILED, version=instance.version + 1)
                self._instances[instance.instance_id] = instance
                self._append_event(instance_id, EventType.REJECTED, instant, step_id=step_id, actor=identity)
                return instance
            approval_count = sum(1 for item in approvals.values() if item == "approve")
            state = StepState.SUCCEEDED if approval_count >= step.required_approvals else StepState.READY
            changed = replace(execution, state=state, approvals=tuple(sorted(approvals.items())), completed_at=instant if state == StepState.SUCCEEDED else None, version=execution.version + 1)
            instance = self._store_step(instance, changed)
            self._append_event(instance_id, EventType.APPROVED, instant, step_id=step_id, actor=identity)
            return self._advance(instance, instant) if state == StepState.SUCCEEDED else instance

    def cancel(self, instance_id: str, *, reason: str, at: datetime, expected_version: int) -> WorkflowInstance:
        instant = utc(at, "cancel time")
        with self._lock:
            current = self._require_instance(instance_id)
            self._check_instance_version(current, expected_version)
            if current.state in {InstanceState.SUCCEEDED, InstanceState.CANCELLED, InstanceState.COMPENSATED}:
                raise ValidationError("completed workflow cannot be cancelled")
            changed = replace(current, state=InstanceState.CANCELLED, cancellation_reason=text(reason, "cancellation reason"), completed_at=instant, version=current.version + 1)
            self._instances[current.instance_id] = changed
            self._append_event(instance_id, EventType.CANCELLED, instant, details=(("reason", reason),))
            return changed

    def instance(self, instance_id: str) -> Optional[WorkflowInstance]:
        with self._lock:
            return self._instances.get(instance_id.strip())

    def events(self, instance_id: str) -> Tuple[WorkflowEvent, ...]:
        with self._lock:
            return tuple(self._events[item] for item in self._instance_events.get(instance_id, ()))

    def ready_steps(self, instance_id: str) -> Tuple[StepExecution, ...]:
        with self._lock:
            instance = self._require_instance(instance_id)
            return tuple(item for item in instance.steps if item.state == StepState.READY)

    def _advance(self, instance: WorkflowInstance, at: datetime) -> WorkflowInstance:
        definition = self._definition(instance)
        executions = {item.step_id: item for item in instance.steps}
        changed = False
        for step in definition.steps:
            execution = executions[step.step_id]
            if execution.state != StepState.PENDING:
                continue
            dependencies = [executions[item] for item in step.dependencies]
            if all(item.state in {StepState.SUCCEEDED, StepState.SKIPPED} for item in dependencies):
                executions[step.step_id] = replace(execution, state=StepState.READY, version=execution.version + 1)
                changed = True
        complete = all(item.state in {StepState.SUCCEEDED, StepState.SKIPPED} for item in executions.values())
        state = InstanceState.SUCCEEDED if complete else InstanceState.RUNNING
        completed_at = at if complete else instance.completed_at
        if changed or complete or instance.state != state:
            instance = replace(instance, steps=tuple(executions[item.step_id] for item in definition.steps), state=state, completed_at=completed_at, version=instance.version + 1)
            self._instances[instance.instance_id] = instance
        if complete:
            self._append_event(instance.instance_id, EventType.COMPLETED, at)
        return instance

    def _store_step(self, instance: WorkflowInstance, execution: StepExecution) -> WorkflowInstance:
        steps = tuple(execution if item.step_id == execution.step_id else item for item in instance.steps)
        changed = replace(instance, steps=steps, version=instance.version + 1)
        self._instances[instance.instance_id] = changed
        return changed

    def _append_event(self, instance_id: str, event_type: EventType, at: datetime, *, step_id: Optional[str] = None, actor: str = "system", details: Tuple[Tuple[str, str], ...] = ()) -> WorkflowEvent:
        sequence = len(self._instance_events[instance_id]) + 1
        event = WorkflowEvent("%s:%06d" % (instance_id, sequence), instance_id, event_type, at, step_id, actor, details)
        self._events[event.event_id] = event
        self._instance_events[instance_id].append(event.event_id)
        return event

    def _require_instance(self, instance_id: str) -> WorkflowInstance:
        value = self._instances.get(text(instance_id, "instance id"))
        if value is None:
            raise NotFoundError("workflow instance does not exist")
        return value

    def _definition(self, instance: WorkflowInstance) -> WorkflowDefinition:
        return self._definitions[(instance.definition_id, instance.definition_version)]

    @staticmethod
    def _step_definition(definition: WorkflowDefinition, step_id: str):
        matches = [item for item in definition.steps if item.step_id == step_id]
        if not matches:
            raise NotFoundError("workflow step does not exist")
        return matches[0]

    @staticmethod
    def _execution(instance: WorkflowInstance, step_id: str) -> StepExecution:
        matches = [item for item in instance.steps if item.step_id == step_id]
        if not matches:
            raise NotFoundError("workflow step execution does not exist")
        return matches[0]

    @staticmethod
    def _check_instance_version(instance: WorkflowInstance, expected_version: int) -> None:
        if instance.version != expected_version:
            raise StorageConflictError("workflow instance version does not match")
