"""Segregated model approval workflow with tier-dependent stages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from typing import Dict, Iterable, Mapping, Tuple

from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError
from forex_monitor.model_governance.models import ApprovalDecision, ApprovalRecord, ModelTier, required


@dataclass(frozen=True)
class ApprovalPolicy:
    tier: ModelTier
    stages: Tuple[str, ...]
    prohibited_self_approval: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "tier", ModelTier(self.tier))
        stages = tuple(item.strip().lower() for item in self.stages if item.strip())
        if not stages or len(stages) != len(set(stages)):
            raise ValidationError("approval stages are empty or duplicated")
        object.__setattr__(self, "stages", stages)


@dataclass(frozen=True)
class ApprovalSummary:
    model_id: str
    model_version: str
    required_stages: Tuple[str, ...]
    approved_stages: Tuple[str, ...]
    rejected_stages: Tuple[str, ...]
    changes_requested: Tuple[str, ...]

    @property
    def approved(self) -> bool:
        return set(self.required_stages) == set(self.approved_stages) and not self.rejected_stages and not self.changes_requested


class ModelApprovalService:
    def __init__(self, policies: Iterable[ApprovalPolicy] = ()) -> None:
        self._policies = {item.tier: item for item in policies}
        self._records: Dict[str, ApprovalRecord] = {}
        self._model_records: Dict[Tuple[str, str], list[str]] = {}
        self._lock = RLock()
        if not self._policies:
            self._policies = {
                ModelTier.LOW: ApprovalPolicy(ModelTier.LOW, ("owner",)),
                ModelTier.MEDIUM: ApprovalPolicy(ModelTier.MEDIUM, ("owner", "independent_validation")),
                ModelTier.HIGH: ApprovalPolicy(ModelTier.HIGH, ("owner", "independent_validation", "model_risk")),
                ModelTier.CRITICAL: ApprovalPolicy(ModelTier.CRITICAL, ("owner", "independent_validation", "model_risk", "risk_committee")),
            }

    def decide(
        self,
        *,
        approval_id: str,
        model_id: str,
        model_version: str,
        tier: ModelTier,
        model_owner: str,
        stage: str,
        approver: str,
        decision: ApprovalDecision,
        decided_at: datetime,
        comment: str,
    ) -> ApprovalRecord:
        policy = self._policies[ModelTier(tier)]
        stage_value = required(stage, "approval stage").lower()
        if stage_value not in policy.stages:
            raise ValidationError("approval stage is not required by policy")
        if policy.prohibited_self_approval and approver == model_owner and stage_value != "owner":
            raise ValidationError("model owner cannot perform independent approval")
        record = ApprovalRecord(approval_id, model_id, model_version, stage_value, approver, decision, decided_at, comment)
        key = (model_id, model_version)
        with self._lock:
            existing = self._records.get(record.approval_id)
            if existing is not None:
                if existing == record:
                    return existing
                raise StorageConflictError("approval id already exists")
            prior = [self._records[item] for item in self._model_records.get(key, ()) if self._records[item].stage == stage_value]
            if prior and prior[-1].decision == ApprovalDecision.APPROVE and record.decision == ApprovalDecision.APPROVE:
                raise StorageConflictError("approval stage is already approved")
            self._records[record.approval_id] = record
            self._model_records.setdefault(key, []).append(record.approval_id)
            return record

    def summary(self, model_id: str, model_version: str, tier: ModelTier) -> ApprovalSummary:
        policy = self._policies[ModelTier(tier)]
        with self._lock:
            values = [self._records[item] for item in self._model_records.get((model_id, model_version), ())]
        latest = {}
        for value in values:
            current = latest.get(value.stage)
            if current is None or value.decided_at > current.decided_at:
                latest[value.stage] = value
        approved = tuple(stage for stage in policy.stages if stage in latest and latest[stage].decision == ApprovalDecision.APPROVE)
        rejected = tuple(stage for stage in policy.stages if stage in latest and latest[stage].decision == ApprovalDecision.REJECT)
        changes = tuple(stage for stage in policy.stages if stage in latest and latest[stage].decision == ApprovalDecision.REQUEST_CHANGES)
        return ApprovalSummary(model_id, model_version, policy.stages, approved, rejected, changes)

    def records(self, model_id: str, model_version: str) -> Tuple[ApprovalRecord, ...]:
        with self._lock:
            return tuple(self._records[item] for item in self._model_records.get((model_id, model_version), ()))
