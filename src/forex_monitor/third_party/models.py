"""Value objects for third-party service governance."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Mapping, Optional, Tuple

from forex_monitor.errors import ValidationError


def required(value: object, name: str, maximum: int = 200) -> str:
    result = str(value).strip() if value is not None else ""
    if not result:
        raise ValidationError("%s is required" % name)
    if len(result) > maximum:
        raise ValidationError("%s exceeds maximum length" % name)
    return result


def utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError("%s must be timezone-aware" % name)
    return value.astimezone(timezone.utc)


def ratio(value: object, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError("%s must be numeric" % name) from exc
    if not result.is_finite() or result < 0 or result > 1:
        raise ValidationError("%s must be between zero and one" % name)
    return result


def amount(value: object, name: str, *, positive: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError("%s must be numeric" % name) from exc
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ValidationError("%s must be %s" % (name, "positive" if positive else "non-negative"))
    return result


class VendorStatus(str, Enum):
    DRAFT = "draft"
    UNDER_REVIEW = "under_review"
    APPROVED = "approved"
    RESTRICTED = "restricted"
    OFFBOARDED = "offboarded"


class Criticality(str, Enum):
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    CRITICAL = "critical"


class ContractStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    EXPIRED = "expired"
    TERMINATED = "terminated"


class MetricKind(str, Enum):
    AVAILABILITY = "availability"
    LATENCY = "latency"
    ERROR_RATE = "error_rate"
    DELIVERY = "delivery"


class MetricDirection(str, Enum):
    AT_LEAST = "at_least"
    AT_MOST = "at_most"


class ReviewStatus(str, Enum):
    OPEN = "open"
    APPROVED = "approved"
    REMEDIATION = "remediation"
    REJECTED = "rejected"
    EXPIRED = "expired"


class IncidentSeverity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class IncidentStatus(str, Enum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    MITIGATING = "mitigating"
    RESOLVED = "resolved"
    CLOSED = "closed"


@dataclass(frozen=True)
class Contact:
    name: str
    email: str
    role: str
    phone: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", required(self.name, "contact name"))
        email = required(self.email, "contact email").lower()
        if "@" not in email or email.startswith("@") or email.endswith("@"):
            raise ValidationError("contact email is invalid")
        object.__setattr__(self, "email", email)
        object.__setattr__(self, "role", required(self.role, "contact role", 80).lower())
        if self.phone is not None:
            object.__setattr__(self, "phone", required(self.phone, "contact phone", 40))


@dataclass(frozen=True)
class Vendor:
    vendor_id: str
    legal_name: str
    service_name: str
    country: str
    criticality: Criticality
    owner: str
    contacts: Tuple[Contact, ...]
    created_at: datetime
    status: VendorStatus = VendorStatus.DRAFT
    data_categories: Tuple[str, ...] = ()
    subcontractors: Tuple[str, ...] = ()
    services: Tuple[str, ...] = ()
    approved_at: Optional[datetime] = None
    offboarded_at: Optional[datetime] = None
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "vendor_id", required(self.vendor_id, "vendor id", 80))
        object.__setattr__(self, "legal_name", required(self.legal_name, "legal name"))
        object.__setattr__(self, "service_name", required(self.service_name, "service name"))
        country = required(self.country, "country", 2).upper()
        if len(country) != 2 or not country.isalpha():
            raise ValidationError("country must be a two-letter code")
        object.__setattr__(self, "country", country)
        object.__setattr__(self, "criticality", Criticality(self.criticality))
        object.__setattr__(self, "owner", required(self.owner, "vendor owner"))
        if not self.contacts:
            raise ValidationError("vendor requires at least one contact")
        emails = [contact.email for contact in self.contacts]
        if len(emails) != len(set(emails)):
            raise ValidationError("vendor contacts contain duplicate emails")
        object.__setattr__(self, "created_at", utc(self.created_at, "created at"))
        object.__setattr__(self, "status", VendorStatus(self.status))
        object.__setattr__(self, "data_categories", _normalized(self.data_categories))
        object.__setattr__(self, "subcontractors", _normalized(self.subcontractors))
        object.__setattr__(self, "services", _normalized(self.services))
        if self.approved_at is not None:
            object.__setattr__(self, "approved_at", utc(self.approved_at, "approved at"))
        if self.offboarded_at is not None:
            object.__setattr__(self, "offboarded_at", utc(self.offboarded_at, "offboarded at"))
        if self.version < 1:
            raise ValidationError("vendor version must be positive")


@dataclass(frozen=True)
class ServiceLevel:
    metric_id: str
    name: str
    kind: MetricKind
    direction: MetricDirection
    target: Decimal
    unit: str
    window_days: int = 30
    credit_rate: Decimal = Decimal("0")
    credit_cap: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        object.__setattr__(self, "metric_id", required(self.metric_id, "metric id", 80))
        object.__setattr__(self, "name", required(self.name, "metric name"))
        object.__setattr__(self, "kind", MetricKind(self.kind))
        object.__setattr__(self, "direction", MetricDirection(self.direction))
        target = amount(self.target, "metric target")
        if self.kind in {MetricKind.AVAILABILITY, MetricKind.ERROR_RATE, MetricKind.DELIVERY} and target > 1:
            raise ValidationError("ratio metric target cannot exceed one")
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "unit", required(self.unit, "metric unit", 30).lower())
        if self.window_days < 1:
            raise ValidationError("metric window must be positive")
        object.__setattr__(self, "credit_rate", ratio(self.credit_rate, "credit rate"))
        object.__setattr__(self, "credit_cap", ratio(self.credit_cap, "credit cap"))

    def met_by(self, value: Decimal) -> bool:
        return value >= self.target if self.direction == MetricDirection.AT_LEAST else value <= self.target


@dataclass(frozen=True)
class ServiceContract:
    contract_id: str
    vendor_id: str
    service: str
    starts_on: date
    ends_on: date
    currency: str
    annual_value: Decimal
    service_levels: Tuple[ServiceLevel, ...]
    notice_days: int = 30
    auto_renews: bool = False
    status: ContractStatus = ContractStatus.DRAFT
    activated_at: Optional[datetime] = None
    terminated_at: Optional[datetime] = None
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "contract_id", required(self.contract_id, "contract id", 80))
        object.__setattr__(self, "vendor_id", required(self.vendor_id, "vendor id", 80))
        object.__setattr__(self, "service", required(self.service, "contract service"))
        if self.ends_on <= self.starts_on:
            raise ValidationError("contract end must follow its start")
        currency = required(self.currency, "contract currency", 3).upper()
        if len(currency) != 3 or not currency.isalpha():
            raise ValidationError("contract currency must be a three-letter code")
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "annual_value", amount(self.annual_value, "annual value", positive=True))
        metric_ids = [metric.metric_id for metric in self.service_levels]
        if not metric_ids or len(metric_ids) != len(set(metric_ids)):
            raise ValidationError("contract needs uniquely identified service levels")
        if self.notice_days < 0:
            raise ValidationError("notice days cannot be negative")
        object.__setattr__(self, "status", ContractStatus(self.status))
        if self.activated_at is not None:
            object.__setattr__(self, "activated_at", utc(self.activated_at, "activated at"))
        if self.terminated_at is not None:
            object.__setattr__(self, "terminated_at", utc(self.terminated_at, "terminated at"))
        if self.version < 1:
            raise ValidationError("contract version must be positive")


@dataclass(frozen=True)
class RiskFinding:
    finding_id: str
    category: str
    description: str
    severity: Criticality
    remediation: str
    due_on: date
    resolved_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "finding_id", required(self.finding_id, "finding id"))
        object.__setattr__(self, "category", required(self.category, "finding category").lower())
        object.__setattr__(self, "description", required(self.description, "finding description", 1000))
        object.__setattr__(self, "severity", Criticality(self.severity))
        object.__setattr__(self, "remediation", required(self.remediation, "finding remediation", 1000))
        if self.resolved_at is not None:
            object.__setattr__(self, "resolved_at", utc(self.resolved_at, "finding resolved at"))


@dataclass(frozen=True)
class RiskAssessment:
    assessment_id: str
    vendor_id: str
    assessed_on: date
    valid_until: date
    assessor: str
    inherent_score: int
    control_score: int
    residual_score: int
    findings: Tuple[RiskFinding, ...] = ()
    status: ReviewStatus = ReviewStatus.OPEN
    notes: str = ""
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "assessment_id", required(self.assessment_id, "assessment id"))
        object.__setattr__(self, "vendor_id", required(self.vendor_id, "vendor id"))
        if self.valid_until <= self.assessed_on:
            raise ValidationError("assessment validity must follow assessment date")
        object.__setattr__(self, "assessor", required(self.assessor, "assessor"))
        for name, score in (("inherent", self.inherent_score), ("control", self.control_score), ("residual", self.residual_score)):
            if not isinstance(score, int) or isinstance(score, bool) or score < 0 or score > 100:
                raise ValidationError("%s score must be an integer from zero to 100" % name)
        ids = [finding.finding_id for finding in self.findings]
        if len(ids) != len(set(ids)):
            raise ValidationError("assessment contains duplicate findings")
        object.__setattr__(self, "status", ReviewStatus(self.status))
        object.__setattr__(self, "notes", self.notes.strip())
        if self.version < 1:
            raise ValidationError("assessment version must be positive")

    @property
    def open_findings(self) -> Tuple[RiskFinding, ...]:
        return tuple(item for item in self.findings if item.resolved_at is None)


@dataclass(frozen=True)
class VendorIncident:
    incident_id: str
    vendor_id: str
    service: str
    severity: IncidentSeverity
    summary: str
    detected_at: datetime
    status: IncidentStatus = IncidentStatus.OPEN
    acknowledged_at: Optional[datetime] = None
    mitigated_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    impact: str = ""
    external_reference: Optional[str] = None
    updates: Tuple[Tuple[datetime, str], ...] = ()
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "incident_id", required(self.incident_id, "incident id"))
        object.__setattr__(self, "vendor_id", required(self.vendor_id, "vendor id"))
        object.__setattr__(self, "service", required(self.service, "incident service"))
        object.__setattr__(self, "severity", IncidentSeverity(self.severity))
        object.__setattr__(self, "summary", required(self.summary, "incident summary", 500))
        detected = utc(self.detected_at, "detected at")
        object.__setattr__(self, "detected_at", detected)
        object.__setattr__(self, "status", IncidentStatus(self.status))
        for attribute in ("acknowledged_at", "mitigated_at", "resolved_at"):
            value = getattr(self, attribute)
            if value is not None:
                normalized = utc(value, attribute.replace("_", " "))
                if normalized < detected:
                    raise ValidationError("incident milestones cannot precede detection")
                object.__setattr__(self, attribute, normalized)
        normalized_updates = tuple((utc(at, "update time"), required(message, "update", 1000)) for at, message in self.updates)
        if tuple(sorted(normalized_updates, key=lambda item: item[0])) != normalized_updates:
            raise ValidationError("incident updates must be chronological")
        object.__setattr__(self, "updates", normalized_updates)
        object.__setattr__(self, "impact", self.impact.strip())
        if self.version < 1:
            raise ValidationError("incident version must be positive")


@dataclass(frozen=True)
class AuditRecord:
    record_id: str
    vendor_id: str
    action: str
    actor: str
    occurred_at: datetime
    details: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "record_id", required(self.record_id, "audit record id"))
        object.__setattr__(self, "vendor_id", required(self.vendor_id, "vendor id"))
        object.__setattr__(self, "action", required(self.action, "audit action").lower())
        object.__setattr__(self, "actor", required(self.actor, "audit actor"))
        object.__setattr__(self, "occurred_at", utc(self.occurred_at, "audit time"))
        object.__setattr__(self, "details", {str(key): str(value) for key, value in self.details.items()})


def _normalized(values: Tuple[str, ...]) -> Tuple[str, ...]:
    return tuple(sorted(set(required(value, "list item").lower() for value in values)))
