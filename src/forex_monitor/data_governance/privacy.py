"""Consent, data-subject requests, legal holds, and erasure evidence."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from threading import RLock
from typing import Dict, Iterable, Mapping, Optional, Tuple

from forex_monitor.data_governance.models import text, utc
from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError


class RequestType(str, Enum):
    ACCESS = "access"
    ERASURE = "erasure"
    RECTIFICATION = "rectification"
    PORTABILITY = "portability"
    RESTRICTION = "restriction"


class RequestStatus(str, Enum):
    RECEIVED = "received"
    VERIFIED = "verified"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    REJECTED = "rejected"


@dataclass(frozen=True)
class Consent:
    consent_id: str
    subject_id: str
    purpose: str
    granted: bool
    effective_at: datetime
    captured_at: datetime
    source: str
    expires_at: Optional[datetime] = None
    supersedes: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "consent_id", text(self.consent_id, "consent id"))
        object.__setattr__(self, "subject_id", text(self.subject_id, "subject id"))
        object.__setattr__(self, "purpose", text(self.purpose, "consent purpose").lower())
        effective = utc(self.effective_at, "effective at")
        captured = utc(self.captured_at, "captured at")
        if effective > captured:
            raise ValidationError("consent effective time cannot follow capture time")
        object.__setattr__(self, "effective_at", effective)
        object.__setattr__(self, "captured_at", captured)
        object.__setattr__(self, "source", text(self.source, "consent source"))
        if self.expires_at is not None:
            expiry = utc(self.expires_at, "expires at")
            if expiry <= effective:
                raise ValidationError("consent expiry must follow effective time")
            object.__setattr__(self, "expires_at", expiry)


@dataclass(frozen=True)
class LegalHold:
    hold_id: str
    subject_id: str
    reason: str
    created_at: datetime
    created_by: str
    dataset_ids: Tuple[str, ...] = ()
    released_at: Optional[datetime] = None
    released_by: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "hold_id", text(self.hold_id, "hold id"))
        object.__setattr__(self, "subject_id", text(self.subject_id, "subject id"))
        object.__setattr__(self, "reason", text(self.reason, "hold reason", 2000))
        object.__setattr__(self, "created_at", utc(self.created_at, "created at"))
        object.__setattr__(self, "created_by", text(self.created_by, "hold creator"))
        object.__setattr__(self, "dataset_ids", tuple(sorted(set(self.dataset_ids))))
        if self.released_at is not None:
            released = utc(self.released_at, "released at")
            if released < self.created_at or not self.released_by:
                raise ValidationError("legal hold release is invalid")
            object.__setattr__(self, "released_at", released)

    @property
    def active(self) -> bool:
        return self.released_at is None


@dataclass(frozen=True)
class SubjectRequest:
    request_id: str
    subject_id: str
    request_type: RequestType
    received_at: datetime
    due_at: datetime
    jurisdiction: str
    status: RequestStatus = RequestStatus.RECEIVED
    verified_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    rejection_reason: Optional[str] = None
    datasets: Tuple[str, ...] = ()
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", text(self.request_id, "request id"))
        object.__setattr__(self, "subject_id", text(self.subject_id, "subject id"))
        object.__setattr__(self, "request_type", RequestType(self.request_type))
        received = utc(self.received_at, "received at")
        due = utc(self.due_at, "due at")
        if due <= received:
            raise ValidationError("subject request due time must follow receipt")
        object.__setattr__(self, "received_at", received)
        object.__setattr__(self, "due_at", due)
        object.__setattr__(self, "jurisdiction", text(self.jurisdiction, "jurisdiction").upper())
        object.__setattr__(self, "status", RequestStatus(self.status))
        if self.verified_at is not None:
            object.__setattr__(self, "verified_at", utc(self.verified_at, "verified at"))
        if self.completed_at is not None:
            object.__setattr__(self, "completed_at", utc(self.completed_at, "completed at"))
        object.__setattr__(self, "datasets", tuple(sorted(set(self.datasets))))


@dataclass(frozen=True)
class ErasureEvidence:
    evidence_id: str
    request_id: str
    dataset_id: str
    record_count: int
    erased_at: datetime
    executor: str
    evidence_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_id", text(self.evidence_id, "evidence id"))
        object.__setattr__(self, "request_id", text(self.request_id, "request id"))
        object.__setattr__(self, "dataset_id", text(self.dataset_id, "dataset id"))
        if self.record_count < 0:
            raise ValidationError("erasure record count cannot be negative")
        object.__setattr__(self, "erased_at", utc(self.erased_at, "erased at"))
        object.__setattr__(self, "executor", text(self.executor, "executor"))
        digest = text(self.evidence_digest, "evidence digest").lower()
        if len(digest) != 64 or any(item not in "0123456789abcdef" for item in digest):
            raise ValidationError("erasure evidence digest must be SHA-256")
        object.__setattr__(self, "evidence_digest", digest)


class PrivacyService:
    def __init__(self) -> None:
        self._consents: Dict[str, Consent] = {}
        self._subject_consents: Dict[Tuple[str, str], list[str]] = {}
        self._holds: Dict[str, LegalHold] = {}
        self._requests: Dict[str, SubjectRequest] = {}
        self._evidence: Dict[str, ErasureEvidence] = {}
        self._request_evidence: Dict[str, list[str]] = {}
        self._lock = RLock()

    def record_consent(self, consent: Consent) -> Consent:
        if not isinstance(consent, Consent):
            raise ValidationError("consent must be Consent")
        with self._lock:
            existing = self._consents.get(consent.consent_id)
            if existing is not None:
                if existing == consent:
                    return existing
                raise StorageConflictError("consent id already exists")
            if consent.supersedes:
                prior = self._consents.get(consent.supersedes)
                if prior is None or prior.subject_id != consent.subject_id or prior.purpose != consent.purpose:
                    raise ValidationError("superseded consent is invalid")
            self._consents[consent.consent_id] = consent
            self._subject_consents.setdefault((consent.subject_id, consent.purpose), []).append(consent.consent_id)
            return consent

    def has_consent(self, subject_id: str, purpose: str, *, at: datetime) -> bool:
        instant = utc(at, "consent time")
        with self._lock:
            values = [self._consents[item] for item in self._subject_consents.get((subject_id, purpose.lower()), ()) if self._consents[item].effective_at <= instant and (self._consents[item].expires_at is None or instant < self._consents[item].expires_at)]
        if not values:
            return False
        values.sort(key=lambda item: (item.effective_at, item.captured_at, item.consent_id))
        return values[-1].granted

    def add_hold(self, hold: LegalHold) -> LegalHold:
        with self._lock:
            existing = self._holds.get(hold.hold_id)
            if existing is not None:
                if existing == hold:
                    return existing
                raise StorageConflictError("legal hold id already exists")
            self._holds[hold.hold_id] = hold
            return hold

    def release_hold(self, hold_id: str, *, released_at: datetime, released_by: str) -> LegalHold:
        with self._lock:
            current = self._holds.get(text(hold_id, "hold id"))
            if current is None:
                raise NotFoundError("legal hold does not exist")
            if not current.active:
                return current
            changed = replace(current, released_at=utc(released_at, "released at"), released_by=text(released_by, "released by"))
            self._holds[current.hold_id] = changed
            return changed

    def active_holds(self, subject_id: str, dataset_id: Optional[str] = None) -> Tuple[LegalHold, ...]:
        with self._lock:
            values = [item for item in self._holds.values() if item.subject_id == subject_id and item.active and (dataset_id is None or not item.dataset_ids or dataset_id in item.dataset_ids)]
            return tuple(sorted(values, key=lambda item: item.created_at))

    def receive_request(self, request: SubjectRequest) -> SubjectRequest:
        with self._lock:
            existing = self._requests.get(request.request_id)
            if existing is not None:
                if existing == request:
                    return existing
                raise StorageConflictError("subject request id already exists")
            self._requests[request.request_id] = request
            self._request_evidence[request.request_id] = []
            return request

    def verify(self, request_id: str, *, verified_at: datetime, expected_version: int) -> SubjectRequest:
        with self._lock:
            current = self._require_request(request_id)
            self._check_version(current, expected_version)
            if current.status != RequestStatus.RECEIVED:
                raise ValidationError("only received requests can be verified")
            changed = replace(current, status=RequestStatus.VERIFIED, verified_at=utc(verified_at, "verified at"), version=current.version + 1)
            self._requests[current.request_id] = changed
            return changed

    def start(self, request_id: str, *, datasets: Iterable[str], expected_version: int) -> SubjectRequest:
        dataset_values = tuple(sorted(set(item.strip() for item in datasets if item.strip())))
        if not dataset_values:
            raise ValidationError("subject request requires affected datasets")
        with self._lock:
            current = self._require_request(request_id)
            self._check_version(current, expected_version)
            if current.status != RequestStatus.VERIFIED:
                raise ValidationError("only verified requests can start")
            blocked = current.request_type == RequestType.ERASURE and any(self.active_holds(current.subject_id, item) for item in dataset_values)
            changed = replace(current, status=RequestStatus.BLOCKED if blocked else RequestStatus.IN_PROGRESS, datasets=dataset_values, version=current.version + 1)
            self._requests[current.request_id] = changed
            return changed

    def add_erasure_evidence(self, evidence: ErasureEvidence) -> ErasureEvidence:
        with self._lock:
            request = self._require_request(evidence.request_id)
            if request.request_type != RequestType.ERASURE or request.status != RequestStatus.IN_PROGRESS:
                raise ValidationError("request is not an active erasure")
            if evidence.dataset_id not in request.datasets:
                raise ValidationError("evidence dataset is outside request scope")
            existing = self._evidence.get(evidence.evidence_id)
            if existing is not None:
                if existing == evidence:
                    return existing
                raise StorageConflictError("erasure evidence id already exists")
            self._evidence[evidence.evidence_id] = evidence
            self._request_evidence[request.request_id].append(evidence.evidence_id)
            return evidence

    def complete(self, request_id: str, *, completed_at: datetime, expected_version: int) -> SubjectRequest:
        with self._lock:
            current = self._require_request(request_id)
            self._check_version(current, expected_version)
            if current.status != RequestStatus.IN_PROGRESS:
                raise ValidationError("only in-progress requests can complete")
            if current.request_type == RequestType.ERASURE:
                evidenced = {self._evidence[item].dataset_id for item in self._request_evidence[current.request_id]}
                if set(current.datasets) != evidenced:
                    raise ValidationError("erasure request lacks evidence for every dataset")
            changed = replace(current, status=RequestStatus.COMPLETED, completed_at=utc(completed_at, "completed at"), version=current.version + 1)
            self._requests[current.request_id] = changed
            return changed

    def overdue(self, at: datetime) -> Tuple[SubjectRequest, ...]:
        instant = utc(at, "overdue time")
        with self._lock:
            values = [item for item in self._requests.values() if item.due_at < instant and item.status not in {RequestStatus.COMPLETED, RequestStatus.REJECTED}]
            return tuple(sorted(values, key=lambda item: item.due_at))

    def _require_request(self, request_id: str) -> SubjectRequest:
        value = self._requests.get(text(request_id, "request id"))
        if value is None:
            raise NotFoundError("subject request does not exist")
        return value

    @staticmethod
    def _check_version(request: SubjectRequest, expected_version: int) -> None:
        if request.version != expected_version:
            raise StorageConflictError("subject request version does not match")
