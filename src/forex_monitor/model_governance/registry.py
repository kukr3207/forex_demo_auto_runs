"""Model and deployment registry with safe promotion and rollback."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from threading import RLock
from typing import Dict, Optional, Tuple

from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError
from forex_monitor.model_governance.models import Deployment, DeploymentState, ModelStatus, ModelVersion, required, utc


class ModelRegistry:
    def __init__(self) -> None:
        self._models: Dict[Tuple[str, str], ModelVersion] = {}
        self._digests: Dict[str, Tuple[str, str]] = {}
        self._deployments: Dict[str, Deployment] = {}
        self._environment_deployments: Dict[str, list[str]] = {}
        self._lock = RLock()

    def register(self, model: ModelVersion) -> ModelVersion:
        if not isinstance(model, ModelVersion):
            raise ValidationError("model must be ModelVersion")
        with self._lock:
            existing = self._models.get(model.key)
            if existing is not None:
                if existing == model:
                    return existing
                raise StorageConflictError("model version already exists")
            digest_owner = self._digests.get(model.artifact_digest)
            if digest_owner is not None and digest_owner != model.key:
                raise StorageConflictError("artifact digest belongs to another model version")
            if model.parent_version and (model.model_id, model.parent_version) not in self._models:
                raise ValidationError("parent model version does not exist")
            self._models[model.key] = model
            self._digests[model.artifact_digest] = model.key
            return model

    def submit_for_validation(self, model_id: str, version: str, *, expected_revision: int) -> ModelVersion:
        with self._lock:
            current = self._require_model(model_id, version)
            self._check_revision(current, expected_revision)
            if current.status not in {ModelStatus.DRAFT, ModelStatus.REJECTED}:
                raise ValidationError("model cannot enter validation from current status")
            changed = replace(current, status=ModelStatus.VALIDATION, revision=current.revision + 1)
            self._models[current.key] = changed
            return changed

    def set_review_outcome(self, model_id: str, version: str, *, approved: bool, expected_revision: int) -> ModelVersion:
        with self._lock:
            current = self._require_model(model_id, version)
            self._check_revision(current, expected_revision)
            if current.status != ModelStatus.VALIDATION:
                raise ValidationError("model is not under validation")
            changed = replace(current, status=ModelStatus.APPROVED if approved else ModelStatus.REJECTED, revision=current.revision + 1)
            self._models[current.key] = changed
            return changed

    def deploy(
        self,
        *,
        deployment_id: str,
        model_id: str,
        version: str,
        environment: str,
        traffic_percent: object,
        created_at: datetime,
        effective_at: datetime,
        baseline_deployment_id: Optional[str] = None,
    ) -> Deployment:
        traffic = Decimal(str(traffic_percent))
        with self._lock:
            existing = self._deployments.get(deployment_id)
            if existing is not None:
                return existing
            model = self._require_model(model_id, version)
            if model.status not in {ModelStatus.APPROVED, ModelStatus.DEPLOYED}:
                raise ValidationError("only approved models can be deployed")
            environment_key = required(environment, "environment").lower()
            active = [self._deployments[item] for item in self._environment_deployments.get(environment_key, ()) if self._deployments[item].state == DeploymentState.ACTIVE]
            if sum((item.traffic_percent for item in active), Decimal("0")) + traffic > 100:
                raise ValidationError("deployment traffic would exceed one hundred percent")
            if baseline_deployment_id is not None:
                baseline = self._deployments.get(baseline_deployment_id)
                if baseline is None or baseline.environment != environment_key:
                    raise ValidationError("baseline deployment is invalid")
            deployment = Deployment(deployment_id, model_id, version, environment_key, traffic, DeploymentState.ACTIVE, created_at, effective_at, baseline_deployment_id)
            self._deployments[deployment.deployment_id] = deployment
            self._environment_deployments.setdefault(environment_key, []).append(deployment.deployment_id)
            self._models[model.key] = replace(model, status=ModelStatus.DEPLOYED, revision=model.revision + 1)
            return deployment

    def change_traffic(self, deployment_id: str, *, traffic_percent: object, expected_revision: int) -> Deployment:
        traffic = Decimal(str(traffic_percent))
        with self._lock:
            current = self._require_deployment(deployment_id)
            if current.revision != expected_revision:
                raise StorageConflictError("deployment revision does not match")
            if current.state != DeploymentState.ACTIVE:
                raise ValidationError("only active deployment traffic can change")
            peers = [self._deployments[item] for item in self._environment_deployments[current.environment] if item != current.deployment_id and self._deployments[item].state == DeploymentState.ACTIVE]
            if sum((item.traffic_percent for item in peers), Decimal("0")) + traffic > 100:
                raise ValidationError("deployment traffic would exceed one hundred percent")
            changed = replace(current, traffic_percent=traffic, revision=current.revision + 1)
            self._deployments[current.deployment_id] = changed
            return changed

    def pause(self, deployment_id: str, *, expected_revision: int) -> Deployment:
        with self._lock:
            current = self._require_deployment(deployment_id)
            if current.revision != expected_revision:
                raise StorageConflictError("deployment revision does not match")
            if current.state != DeploymentState.ACTIVE:
                raise ValidationError("only active deployments can be paused")
            changed = replace(current, state=DeploymentState.PAUSED, traffic_percent=Decimal("0"), revision=current.revision + 1)
            self._deployments[current.deployment_id] = changed
            return changed

    def rollback(self, deployment_id: str, *, reason: str, expected_revision: int) -> Deployment:
        explanation = required(reason, "rollback reason")
        with self._lock:
            current = self._require_deployment(deployment_id)
            if current.revision != expected_revision:
                raise StorageConflictError("deployment revision does not match")
            if current.state not in {DeploymentState.ACTIVE, DeploymentState.PAUSED}:
                raise ValidationError("deployment cannot be rolled back")
            changed = replace(current, state=DeploymentState.ROLLED_BACK, traffic_percent=Decimal("0"), rollback_reason=explanation, revision=current.revision + 1)
            self._deployments[current.deployment_id] = changed
            return changed

    def retire(self, model_id: str, version: str, *, expected_revision: int) -> ModelVersion:
        with self._lock:
            model = self._require_model(model_id, version)
            self._check_revision(model, expected_revision)
            active = [item for item in self._deployments.values() if item.model_id == model_id and item.model_version == version and item.state == DeploymentState.ACTIVE]
            if active:
                raise ValidationError("model has active deployments")
            changed = replace(model, status=ModelStatus.RETIRED, revision=model.revision + 1)
            self._models[model.key] = changed
            return changed

    def model(self, model_id: str, version: str) -> Optional[ModelVersion]:
        with self._lock:
            return self._models.get((model_id.strip(), version.strip()))

    def versions(self, model_id: str) -> Tuple[ModelVersion, ...]:
        with self._lock:
            return tuple(sorted((item for item in self._models.values() if item.model_id == model_id), key=lambda item: item.created_at))

    def deployment(self, deployment_id: str) -> Optional[Deployment]:
        with self._lock:
            return self._deployments.get(deployment_id.strip())

    def deployments(self, environment: Optional[str] = None) -> Tuple[Deployment, ...]:
        with self._lock:
            values = [item for item in self._deployments.values() if environment is None or item.environment == environment.lower()]
            return tuple(sorted(values, key=lambda item: (item.effective_at, item.deployment_id)))

    def _require_model(self, model_id: str, version: str) -> ModelVersion:
        value = self._models.get((required(model_id, "model id"), required(version, "model version")))
        if value is None:
            raise NotFoundError("model version does not exist")
        return value

    def _require_deployment(self, deployment_id: str) -> Deployment:
        value = self._deployments.get(required(deployment_id, "deployment id"))
        if value is None:
            raise NotFoundError("deployment does not exist")
        return value

    @staticmethod
    def _check_revision(model: ModelVersion, expected_revision: int) -> None:
        if model.revision != expected_revision:
            raise StorageConflictError("model revision does not match")
