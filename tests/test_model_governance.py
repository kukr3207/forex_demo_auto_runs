from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from forex_monitor.errors import StorageConflictError, ValidationError
from forex_monitor.model_governance.approvals import ModelApprovalService
from forex_monitor.model_governance.models import ApprovalDecision, FeatureDefinition, MetricRequirement, ModelStatus, ModelTier, ModelVersion, MonitoringObservation
from forex_monitor.model_governance.monitoring import ModelMonitor
from forex_monitor.model_governance.registry import ModelRegistry
from forex_monitor.model_governance.validation import ModelValidator


NOW = datetime(2026, 10, 12, 8, tzinfo=timezone.utc)
DIGEST = "a" * 64


def model(tier=ModelTier.HIGH):
    return ModelVersion("fx-signal", "1.0", "FX Signal", "quant-team", "Predict next-hour direction", tier, "gradient_boosting", "s3://models/fx-signal", DIGEST, "dataset-1", NOW - timedelta(days=1), NOW, (FeatureDefinition("return_1h", "decimal", "market-data"), FeatureDefinition("session", "category", "calendar", categories=("asia", "europe", "us"))), (MetricRequirement("accuracy", ">=", Decimal("0.60")),))


def test_registry_promotes_approved_model_and_controls_traffic() -> None:
    registry = ModelRegistry()
    draft = registry.register(model())
    validating = registry.submit_for_validation(draft.model_id, draft.version, expected_revision=1)
    approved = registry.set_review_outcome(draft.model_id, draft.version, approved=True, expected_revision=validating.revision)
    deployment = registry.deploy(deployment_id="prod-1", model_id=draft.model_id, version=draft.version, environment="production", traffic_percent=20, created_at=NOW, effective_at=NOW)
    assert approved.status == ModelStatus.APPROVED
    assert deployment.traffic_percent == Decimal("20")
    assert registry.model(draft.model_id, draft.version).status == ModelStatus.DEPLOYED


def test_registry_rejects_duplicate_artifact_digest() -> None:
    registry = ModelRegistry()
    registry.register(model())
    other = model()
    other = type(other)(**{**other.__dict__, "model_id": "other"})
    with pytest.raises(StorageConflictError):
        registry.register(other)


def test_deployment_traffic_cannot_exceed_one_hundred() -> None:
    registry = ModelRegistry()
    registry.register(model())
    validation = registry.submit_for_validation("fx-signal", "1.0", expected_revision=1)
    registry.set_review_outcome("fx-signal", "1.0", approved=True, expected_revision=validation.revision)
    registry.deploy(deployment_id="a", model_id="fx-signal", version="1.0", environment="prod", traffic_percent=80, created_at=NOW, effective_at=NOW)
    with pytest.raises(ValidationError):
        registry.deploy(deployment_id="b", model_id="fx-signal", version="1.0", environment="prod", traffic_percent=30, created_at=NOW, effective_at=NOW)


def test_regression_and_classification_metrics_are_computed() -> None:
    regression = {item.name: item.value for item in ModelValidator.regression_metrics((1, 2, 3), (1, 2, 4))}
    assert regression["mae"] == Decimal("0.3333333333333333333333333333")
    classification = {item.name: item.value for item in ModelValidator.classification_metrics((True, False, True, False), ("0.9", "0.8", "0.7", "0.1"))}
    assert classification["accuracy"] == Decimal("0.75")
    assert classification["precision"] == Decimal("0.6666666666666666666666666667")


def test_validation_gate_reports_missing_and_failed_metrics() -> None:
    validator = ModelValidator()
    value = model()
    metrics = ModelValidator.classification_metrics((True, False), ("0.4", "0.6"))
    outcome = validator.evaluate(run_id="run", model=value, dataset_id="holdout", metrics=metrics, started_at=NOW, completed_at=NOW)
    assert not outcome.run.passed
    assert outcome.failures[0].name == "accuracy"
    assert validator.run("run") == outcome


def test_tiered_approval_requires_independent_review() -> None:
    approvals = ModelApprovalService()
    approvals.decide(approval_id="owner", model_id="fx-signal", model_version="1.0", tier=ModelTier.HIGH, model_owner="alice", stage="owner", approver="alice", decision=ApprovalDecision.APPROVE, decided_at=NOW, comment="Ready")
    with pytest.raises(ValidationError):
        approvals.decide(approval_id="self", model_id="fx-signal", model_version="1.0", tier=ModelTier.HIGH, model_owner="alice", stage="model_risk", approver="alice", decision=ApprovalDecision.APPROVE, decided_at=NOW, comment="Self approve")
    approvals.decide(approval_id="validation", model_id="fx-signal", model_version="1.0", tier=ModelTier.HIGH, model_owner="alice", stage="independent_validation", approver="bob", decision=ApprovalDecision.APPROVE, decided_at=NOW, comment="Validated")
    approvals.decide(approval_id="risk", model_id="fx-signal", model_version="1.0", tier=ModelTier.HIGH, model_owner="alice", stage="model_risk", approver="carol", decision=ApprovalDecision.APPROVE, decided_at=NOW, comment="Approved")
    assert approvals.summary("fx-signal", "1.0", ModelTier.HIGH).approved


def test_monitor_emits_performance_and_drift_alerts() -> None:
    monitor = ModelMonitor()
    monitor.configure("prod", requirements=(MetricRequirement("accuracy", ">=", Decimal("0.6")),), baseline_distributions={"return": (80, 20)})
    observation = MonitoringObservation("obs", "prod", NOW, 100, (("accuracy", Decimal("0.5")),), (("return", (Decimal("20"), Decimal("80"))),))
    alerts = monitor.observe(observation)
    assert {item.alert_type for item in alerts} == {"performance", "feature_drift"}
    assert len(monitor.alerts("prod", open_only=True)) == 2


def test_monitor_replay_and_alert_resolution_are_idempotent() -> None:
    monitor = ModelMonitor()
    monitor.configure("prod", requirements=(MetricRequirement("accuracy", ">=", Decimal("0.6")),))
    observation = MonitoringObservation("obs", "prod", NOW, 10, (("accuracy", Decimal("0.5")),))
    first = monitor.observe(observation)
    assert monitor.observe(observation) == first
    resolved = monitor.resolve(first[0].alert_id, resolved_at=NOW + timedelta(hours=1))
    assert monitor.resolve(first[0].alert_id, resolved_at=NOW + timedelta(hours=2)) == resolved
    assert not monitor.alerts("prod", open_only=True)
