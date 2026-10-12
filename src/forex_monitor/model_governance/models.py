"""Immutable model-risk and deployment records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Mapping, Optional, Tuple

from forex_monitor.errors import ValidationError


def required(value: object, name: str, maximum: Optional[int] = None) -> str:
    result = str(value).strip() if value is not None else ""
    if not result:
        raise ValidationError("%s is required" % name)
    if maximum is not None and len(result) > maximum:
        raise ValidationError("%s exceeds maximum length" % name)
    return result


def number(value: object, name: str, *, non_negative: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError("%s must be numeric" % name) from exc
    if not result.is_finite():
        raise ValidationError("%s must be finite" % name)
    if non_negative and result < 0:
        raise ValidationError("%s cannot be negative" % name)
    return result


def utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError("%s must be timezone-aware" % name)
    return value.astimezone(timezone.utc)


class ModelStatus(str, Enum):
    DRAFT = "draft"
    VALIDATION = "validation"
    APPROVED = "approved"
    REJECTED = "rejected"
    DEPLOYED = "deployed"
    RETIRED = "retired"


class ModelTier(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ApprovalDecision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    REQUEST_CHANGES = "request_changes"


class DeploymentState(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    PAUSED = "paused"
    ROLLED_BACK = "rolled_back"
    COMPLETED = "completed"


class AlertSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True)
class FeatureDefinition:
    name: str
    data_type: str
    source: str
    required: bool = True
    minimum: Optional[Decimal] = None
    maximum: Optional[Decimal] = None
    categories: Tuple[str, ...] = ()
    default: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", required(self.name, "feature name", 100))
        data_type = required(self.data_type, "feature data type").lower()
        if data_type not in {"decimal", "integer", "boolean", "string", "category", "datetime"}:
            raise ValidationError("feature data type is invalid")
        object.__setattr__(self, "data_type", data_type)
        object.__setattr__(self, "source", required(self.source, "feature source", 200))
        if self.minimum is not None:
            object.__setattr__(self, "minimum", number(self.minimum, "feature minimum"))
        if self.maximum is not None:
            object.__setattr__(self, "maximum", number(self.maximum, "feature maximum"))
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValidationError("feature minimum exceeds maximum")
        categories = tuple(sorted(set(item.strip() for item in self.categories if item.strip())))
        if data_type == "category" and not categories:
            raise ValidationError("categorical feature requires categories")
        object.__setattr__(self, "categories", categories)


@dataclass(frozen=True)
class MetricRequirement:
    name: str
    operator: str
    threshold: Decimal
    segment: Optional[str] = None
    severity: AlertSeverity = AlertSeverity.CRITICAL

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", required(self.name, "metric name"))
        operator = required(self.operator, "metric operator")
        if operator not in {">", ">=", "<", "<=", "==", "!="}:
            raise ValidationError("metric operator is invalid")
        object.__setattr__(self, "operator", operator)
        object.__setattr__(self, "threshold", number(self.threshold, "metric threshold"))
        object.__setattr__(self, "severity", AlertSeverity(self.severity))

    def satisfied_by(self, value: Decimal) -> bool:
        return {
            ">": value > self.threshold,
            ">=": value >= self.threshold,
            "<": value < self.threshold,
            "<=": value <= self.threshold,
            "==": value == self.threshold,
            "!=": value != self.threshold,
        }[self.operator]


@dataclass(frozen=True)
class ModelVersion:
    model_id: str
    version: str
    name: str
    owner: str
    purpose: str
    tier: ModelTier
    algorithm: str
    artifact_uri: str
    artifact_digest: str
    training_data_id: str
    trained_at: datetime
    created_at: datetime
    features: Tuple[FeatureDefinition, ...]
    requirements: Tuple[MetricRequirement, ...]
    status: ModelStatus = ModelStatus.DRAFT
    parent_version: Optional[str] = None
    tags: Tuple[str, ...] = ()
    metadata: Tuple[Tuple[str, str], ...] = ()
    revision: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "model_id", required(self.model_id, "model id", 80))
        object.__setattr__(self, "version", required(self.version, "model version", 40))
        object.__setattr__(self, "name", required(self.name, "model name", 160))
        object.__setattr__(self, "owner", required(self.owner, "model owner", 160))
        object.__setattr__(self, "purpose", required(self.purpose, "model purpose", 1000))
        object.__setattr__(self, "tier", ModelTier(self.tier))
        object.__setattr__(self, "algorithm", required(self.algorithm, "algorithm", 200))
        object.__setattr__(self, "artifact_uri", required(self.artifact_uri, "artifact URI", 500))
        digest = required(self.artifact_digest, "artifact digest").lower()
        if len(digest) != 64 or any(item not in "0123456789abcdef" for item in digest):
            raise ValidationError("artifact digest must be SHA-256")
        object.__setattr__(self, "artifact_digest", digest)
        object.__setattr__(self, "training_data_id", required(self.training_data_id, "training data id"))
        trained = utc(self.trained_at, "trained at")
        created = utc(self.created_at, "created at")
        if trained > created:
            raise ValidationError("training time cannot follow registration")
        object.__setattr__(self, "trained_at", trained)
        object.__setattr__(self, "created_at", created)
        feature_names = [item.name for item in self.features]
        if not feature_names or len(feature_names) != len(set(feature_names)):
            raise ValidationError("model features are empty or duplicated")
        requirement_keys = [(item.name, item.segment) for item in self.requirements]
        if len(requirement_keys) != len(set(requirement_keys)):
            raise ValidationError("model metric requirements are duplicated")
        object.__setattr__(self, "status", ModelStatus(self.status))
        object.__setattr__(self, "tags", tuple(sorted(set(item.strip().lower() for item in self.tags if item.strip()))))
        metadata_keys = [key for key, _ in self.metadata]
        if len(metadata_keys) != len(set(metadata_keys)):
            raise ValidationError("model metadata keys are duplicated")
        object.__setattr__(self, "metadata", tuple(sorted(self.metadata)))
        if self.revision < 1:
            raise ValidationError("model revision must be positive")

    @property
    def key(self) -> Tuple[str, str]:
        return self.model_id, self.version


@dataclass(frozen=True)
class ValidationMetric:
    name: str
    value: Decimal
    sample_count: int
    segment: Optional[str] = None
    confidence_low: Optional[Decimal] = None
    confidence_high: Optional[Decimal] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", required(self.name, "metric name"))
        object.__setattr__(self, "value", number(self.value, "metric value"))
        if self.sample_count < 1:
            raise ValidationError("metric sample count must be positive")
        if self.confidence_low is not None:
            object.__setattr__(self, "confidence_low", number(self.confidence_low, "confidence low"))
        if self.confidence_high is not None:
            object.__setattr__(self, "confidence_high", number(self.confidence_high, "confidence high"))
        if self.confidence_low is not None and self.confidence_high is not None and self.confidence_low > self.confidence_high:
            raise ValidationError("metric confidence interval is invalid")


@dataclass(frozen=True)
class ValidationRun:
    run_id: str
    model_id: str
    model_version: str
    dataset_id: str
    started_at: datetime
    completed_at: datetime
    metrics: Tuple[ValidationMetric, ...]
    passed: bool
    findings: Tuple[str, ...] = ()
    validator: str = "automated"

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", required(self.run_id, "validation run id"))
        object.__setattr__(self, "model_id", required(self.model_id, "model id"))
        object.__setattr__(self, "model_version", required(self.model_version, "model version"))
        object.__setattr__(self, "dataset_id", required(self.dataset_id, "dataset id"))
        started = utc(self.started_at, "started at")
        completed = utc(self.completed_at, "completed at")
        if completed < started:
            raise ValidationError("validation completion precedes start")
        object.__setattr__(self, "started_at", started)
        object.__setattr__(self, "completed_at", completed)
        keys = [(item.name, item.segment) for item in self.metrics]
        if len(keys) != len(set(keys)):
            raise ValidationError("validation metrics are duplicated")


@dataclass(frozen=True)
class ApprovalRecord:
    approval_id: str
    model_id: str
    model_version: str
    stage: str
    approver: str
    decision: ApprovalDecision
    decided_at: datetime
    comment: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "approval_id", required(self.approval_id, "approval id"))
        object.__setattr__(self, "model_id", required(self.model_id, "model id"))
        object.__setattr__(self, "model_version", required(self.model_version, "model version"))
        object.__setattr__(self, "stage", required(self.stage, "approval stage").lower())
        object.__setattr__(self, "approver", required(self.approver, "approver"))
        object.__setattr__(self, "decision", ApprovalDecision(self.decision))
        object.__setattr__(self, "decided_at", utc(self.decided_at, "decided at"))
        object.__setattr__(self, "comment", required(self.comment, "approval comment", 2000))


@dataclass(frozen=True)
class Deployment:
    deployment_id: str
    model_id: str
    model_version: str
    environment: str
    traffic_percent: Decimal
    state: DeploymentState
    created_at: datetime
    effective_at: datetime
    baseline_deployment_id: Optional[str] = None
    rollback_reason: Optional[str] = None
    revision: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "deployment_id", required(self.deployment_id, "deployment id"))
        object.__setattr__(self, "model_id", required(self.model_id, "model id"))
        object.__setattr__(self, "model_version", required(self.model_version, "model version"))
        object.__setattr__(self, "environment", required(self.environment, "environment").lower())
        traffic = number(self.traffic_percent, "traffic percent", non_negative=True)
        if traffic > 100:
            raise ValidationError("traffic percent cannot exceed one hundred")
        object.__setattr__(self, "traffic_percent", traffic)
        object.__setattr__(self, "state", DeploymentState(self.state))
        created = utc(self.created_at, "created at")
        effective = utc(self.effective_at, "effective at")
        if effective < created:
            raise ValidationError("deployment effective time precedes creation")
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "effective_at", effective)


@dataclass(frozen=True)
class MonitoringObservation:
    observation_id: str
    deployment_id: str
    observed_at: datetime
    sample_count: int
    metrics: Tuple[Tuple[str, Decimal], ...]
    feature_distributions: Tuple[Tuple[str, Tuple[Decimal, ...]], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "observation_id", required(self.observation_id, "observation id"))
        object.__setattr__(self, "deployment_id", required(self.deployment_id, "deployment id"))
        object.__setattr__(self, "observed_at", utc(self.observed_at, "observed at"))
        if self.sample_count < 1:
            raise ValidationError("monitoring sample count must be positive")
        names = [name for name, _ in self.metrics]
        if len(names) != len(set(names)):
            raise ValidationError("monitoring metrics are duplicated")
        object.__setattr__(self, "metrics", tuple(sorted((name, number(value, name)) for name, value in self.metrics)))
        feature_names = [name for name, _ in self.feature_distributions]
        if len(feature_names) != len(set(feature_names)):
            raise ValidationError("feature distributions are duplicated")
        object.__setattr__(self, "feature_distributions", tuple(sorted(self.feature_distributions)))


@dataclass(frozen=True)
class ModelAlert:
    alert_id: str
    deployment_id: str
    alert_type: str
    severity: AlertSeverity
    metric: str
    observed_value: Decimal
    threshold: Decimal
    created_at: datetime
    message: str
    resolved_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "alert_id", required(self.alert_id, "alert id"))
        object.__setattr__(self, "deployment_id", required(self.deployment_id, "deployment id"))
        object.__setattr__(self, "alert_type", required(self.alert_type, "alert type").lower())
        object.__setattr__(self, "severity", AlertSeverity(self.severity))
        object.__setattr__(self, "metric", required(self.metric, "metric"))
        object.__setattr__(self, "observed_value", number(self.observed_value, "observed value"))
        object.__setattr__(self, "threshold", number(self.threshold, "threshold"))
        object.__setattr__(self, "created_at", utc(self.created_at, "created at"))
        object.__setattr__(self, "message", required(self.message, "alert message"))
        if self.resolved_at is not None:
            resolved = utc(self.resolved_at, "resolved at")
            if resolved < self.created_at:
                raise ValidationError("alert resolution precedes creation")
            object.__setattr__(self, "resolved_at", resolved)
