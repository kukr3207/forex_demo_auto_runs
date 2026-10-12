from datetime import datetime, timedelta, timezone

import pytest

from forex_monitor.errors import StorageConflictError, ValidationError
from forex_monitor.workflow.engine import WorkflowEngine
from forex_monitor.workflow.models import InstanceState, RetryPolicy, StepState, StepType, WorkflowDefinition, WorkflowStep
from forex_monitor.workflow.schedules import MisfirePolicy, WorkflowSchedule, WorkflowScheduler


NOW = datetime(2026, 10, 12, 8, tzinfo=timezone.utc)


def definition():
    return WorkflowDefinition("trade-approval", 1, "Trade approval", (
        WorkflowStep("validate", "Validate trade", StepType.TASK, "validate", retry_policy=RetryPolicy(3, timedelta(seconds=10), retryable_errors=("temporary",))),
        WorkflowStep("approve", "Approve trade", StepType.APPROVAL, "approval", dependencies=("validate",), approval_roles=("supervisor", "risk"), required_approvals=2),
        WorkflowStep("publish", "Publish trade", StepType.TASK, "publish", dependencies=("approve",)),
    ), NOW, "ops")


def started():
    engine = WorkflowEngine()
    engine.register(definition())
    instance = engine.start(instance_id="instance", definition_id="trade-approval", correlation_id="trade-1", input={"trade": "1"}, started_at=NOW)
    return engine, instance


def test_workflow_executes_dependencies_and_approval_quorum() -> None:
    engine, instance = started()
    engine.claim_step(instance.instance_id, "validate", at=NOW, expected_version=instance.version)
    instance = engine.instance(instance.instance_id)
    instance = engine.succeed_step(instance.instance_id, "validate", output={"valid": True}, at=NOW, expected_version=instance.version)
    assert engine.ready_steps(instance.instance_id)[0].step_id == "approve"
    instance = engine.approve(instance.instance_id, "approve", approver="alice", role="supervisor", approved=True, at=NOW)
    assert engine.ready_steps(instance.instance_id)[0].step_id == "approve"
    instance = engine.approve(instance.instance_id, "approve", approver="bob", role="risk", approved=True, at=NOW)
    assert engine.ready_steps(instance.instance_id)[0].step_id == "publish"


def test_workflow_retry_waits_and_becomes_ready_on_tick() -> None:
    engine, instance = started()
    engine.claim_step(instance.instance_id, "validate", at=NOW, expected_version=instance.version)
    current = engine.instance(instance.instance_id)
    waiting = engine.fail_step(current.instance_id, "validate", error_code="temporary", error_message="Provider timeout", at=NOW, expected_version=current.version)
    assert waiting.state == InstanceState.WAITING
    assert engine.tick(waiting.instance_id, at=NOW + timedelta(seconds=9)).state == InstanceState.WAITING
    ready = engine.tick(waiting.instance_id, at=NOW + timedelta(seconds=10))
    assert ready.state == InstanceState.RUNNING
    assert engine.ready_steps(ready.instance_id)[0].step_id == "validate"


def test_non_retryable_failure_fails_instance() -> None:
    engine, instance = started()
    engine.claim_step(instance.instance_id, "validate", at=NOW, expected_version=instance.version)
    current = engine.instance(instance.instance_id)
    failed = engine.fail_step(current.instance_id, "validate", error_code="invalid", error_message="Bad trade", at=NOW, expected_version=current.version)
    assert failed.state == InstanceState.FAILED


def test_start_is_idempotent_by_definition_and_correlation() -> None:
    engine, first = started()
    second = engine.start(instance_id="other", definition_id="trade-approval", correlation_id="trade-1", input={"different": "ignored"}, started_at=NOW)
    assert second == first


def test_definition_rejects_cycle() -> None:
    with pytest.raises(ValidationError):
        WorkflowDefinition("cycle", 1, "Cycle", (WorkflowStep("a", "A", StepType.TASK, "a", dependencies=("b",)), WorkflowStep("b", "B", StepType.TASK, "b", dependencies=("a",))), NOW, "ops")


def test_signal_step_consumes_one_idempotent_signal() -> None:
    engine = WorkflowEngine()
    engine.register(WorkflowDefinition("signal", 1, "Signal", (WorkflowStep("wait", "Wait", StepType.SIGNAL, "signal", signal_name="settled"),), NOW, "ops"))
    instance = engine.start(instance_id="signal-instance", definition_id="signal", correlation_id="one", input={}, started_at=NOW)
    completed = engine.signal(instance.instance_id, "settled", signal_id="signal-1", payload={"reference": "abc"}, at=NOW)
    assert completed.state == InstanceState.SUCCEEDED
    assert engine.signal(instance.instance_id, "settled", signal_id="signal-1", payload={"reference": "abc"}, at=NOW) == completed


def test_rejected_approval_fails_workflow() -> None:
    engine, instance = started()
    engine.claim_step(instance.instance_id, "validate", at=NOW, expected_version=instance.version)
    current = engine.instance(instance.instance_id)
    current = engine.succeed_step(current.instance_id, "validate", output={}, at=NOW, expected_version=current.version)
    rejected = engine.approve(current.instance_id, "approve", approver="alice", role="risk", approved=False, at=NOW)
    assert rejected.state == InstanceState.FAILED


def test_scheduler_catches_up_missed_intervals() -> None:
    engine = WorkflowEngine()
    engine.register(WorkflowDefinition("simple", 1, "Simple", (WorkflowStep("one", "One", StepType.TASK, "one"),), NOW, "ops"))
    scheduler = WorkflowScheduler(engine)
    scheduler.add(WorkflowSchedule("hourly", "simple", timedelta(hours=1), NOW, misfire_policy=MisfirePolicy.CATCH_UP, maximum_catch_up=3))
    runs = scheduler.dispatch(NOW + timedelta(hours=5))
    assert len(runs) == 3
    assert len(scheduler.runs("hourly")) == 3


def test_scheduler_prevents_overlap_by_default() -> None:
    engine = WorkflowEngine()
    engine.register(WorkflowDefinition("simple", 1, "Simple", (WorkflowStep("one", "One", StepType.TASK, "one"),), NOW, "ops"))
    scheduler = WorkflowScheduler(engine)
    scheduler.add(WorkflowSchedule("hourly", "simple", timedelta(hours=1), NOW))
    assert len(scheduler.dispatch(NOW)) == 1
    assert not scheduler.dispatch(NOW + timedelta(hours=1))
