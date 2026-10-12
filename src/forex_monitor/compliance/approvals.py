"""Four-eyes and threshold-based approval workflow."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Dict, Mapping, Optional, Sequence, Tuple

from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError
from forex_monitor.models import as_decimal, ensure_utc


class ApprovalState(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class ApprovalPolicy:
    policy_id: str
    minimum_approvals: int = 1
    prohibited_self_approval: bool = True
    required_roles: Tuple[str, ...] = ()
    expires_after: timedelta = timedelta(hours=4)
    notional_threshold: Optional[Decimal] = None

    def __post_init__(self) -> None:
        if not self.policy_id.strip():
            raise ValidationError("approval policy id cannot be blank")
        if self.minimum_approvals <= 0:
            raise ValidationError("minimum approvals must be positive")
        if self.expires_after.total_seconds() <= 0:
            raise ValidationError("approval expiry must be positive")
        roles = tuple(sorted({role.strip() for role in self.required_roles if role.strip()}))
        if roles and self.minimum_approvals < len(roles):
            raise ValidationError("minimum approvals cannot be below required role count")
        object.__setattr__(self, "required_roles", roles)
        if self.notional_threshold is not None:
            object.__setattr__(self, "notional_threshold", as_decimal(self.notional_threshold, "notional threshold", positive=True))


@dataclass(frozen=True)
class ApprovalDecision:
    approver_id: str
    role: str
    approved: bool
    decided_at: datetime
    reason: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.approver_id.strip() or not self.role.strip():
            raise ValidationError("approver id and role cannot be blank")
        object.__setattr__(self, "decided_at", ensure_utc(self.decided_at, "approval time"))
        if not self.approved and not (self.reason and self.reason.strip()):
            raise ValidationError("rejected approval decisions require a reason")


@dataclass(frozen=True)
class ApprovalRequest:
    request_id: str
    subject_type: str
    subject_id: str
    requester_id: str
    policy_id: str
    requested_at: datetime
    expires_at: datetime
    state: ApprovalState = ApprovalState.PENDING
    decisions: Tuple[ApprovalDecision, ...] = ()
    payload_digest: Optional[str] = None

    def __post_init__(self) -> None:
        if not all((self.request_id.strip(), self.subject_type.strip(), self.subject_id.strip(), self.requester_id.strip(), self.policy_id.strip())):
            raise ValidationError("approval request fields cannot be blank")
        requested = ensure_utc(self.requested_at, "request time")
        expires = ensure_utc(self.expires_at, "request expiry")
        if expires <= requested:
            raise ValidationError("approval request expiry must follow request time")
        object.__setattr__(self, "requested_at", requested)
        object.__setattr__(self, "expires_at", expires)
        object.__setattr__(self, "state", self.state if isinstance(self.state, ApprovalState) else ApprovalState(self.state))


class ApprovalWorkflow:
    def __init__(self) -> None:
        self._policies: Dict[str, ApprovalPolicy] = {}
        self._requests: Dict[str, ApprovalRequest] = {}

    def add_policy(self, policy: ApprovalPolicy) -> None:
        if policy.policy_id in self._policies:
            raise StorageConflictError("approval policy already exists")
        self._policies[policy.policy_id] = policy

    def request(
        self,
        request_id: str,
        subject_type: str,
        subject_id: str,
        requester_id: str,
        policy_id: str,
        at: datetime,
        *,
        payload_digest: Optional[str] = None,
    ) -> ApprovalRequest:
        try:
            policy = self._policies[policy_id]
        except KeyError as exc:
            raise NotFoundError("approval policy was not found") from exc
        timestamp = ensure_utc(at, "request time")
        value = ApprovalRequest(
            request_id, subject_type, subject_id, requester_id, policy_id,
            timestamp, timestamp + policy.expires_after,
            payload_digest=payload_digest,
        )
        existing = self._requests.get(request_id)
        if existing is not None:
            if existing == value:
                return existing
            raise StorageConflictError("approval request id already exists")
        self._requests[request_id] = value
        return value

    def get(self, request_id: str) -> ApprovalRequest:
        try:
            return self._requests[request_id]
        except KeyError as exc:
            raise NotFoundError("approval request was not found") from exc

    def decide(self, request_id: str, decision: ApprovalDecision) -> ApprovalRequest:
        request = self.get(request_id)
        policy = self._policies[request.policy_id]
        if request.state is not ApprovalState.PENDING:
            raise ValidationError("approval request is no longer pending")
        if decision.decided_at > request.expires_at:
            changed = replace(request, state=ApprovalState.EXPIRED)
            self._requests[request_id] = changed
            raise ValidationError("approval request has expired")
        if policy.prohibited_self_approval and decision.approver_id == request.requester_id:
            raise ValidationError("requester cannot approve their own request")
        if any(item.approver_id == decision.approver_id for item in request.decisions):
            raise StorageConflictError("approver already decided this request")
        decisions = request.decisions + (decision,)
        if not decision.approved:
            state = ApprovalState.REJECTED
        else:
            approvals = [item for item in decisions if item.approved]
            roles = {item.role for item in approvals}
            roles_satisfied = all(role in roles for role in policy.required_roles)
            state = ApprovalState.APPROVED if len(approvals) >= policy.minimum_approvals and roles_satisfied else ApprovalState.PENDING
        changed = replace(request, decisions=decisions, state=state)
        self._requests[request_id] = changed
        return changed

    def expire(self, now: datetime) -> Tuple[ApprovalRequest, ...]:
        timestamp = ensure_utc(now, "approval expiry time")
        expired = []
        for request_id, request in tuple(self._requests.items()):
            if request.state is ApprovalState.PENDING and timestamp > request.expires_at:
                changed = replace(request, state=ApprovalState.EXPIRED)
                self._requests[request_id] = changed
                expired.append(changed)
        return tuple(expired)
