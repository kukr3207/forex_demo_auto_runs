"""Risk reviews and portfolio concentration analysis for third parties."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from threading import RLock
from typing import Dict, Iterable, Mapping, Optional, Tuple

from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError
from forex_monitor.third_party.models import (
    Criticality,
    ReviewStatus,
    RiskAssessment,
    RiskFinding,
    Vendor,
    required,
    utc,
)


@dataclass(frozen=True)
class PortfolioRisk:
    vendor_count: int
    critical_vendor_count: int
    overdue_review_count: int
    open_finding_count: int
    highest_country_share: Decimal
    highest_service_share: Decimal
    concentration_warnings: Tuple[str, ...]


class RiskManager:
    def __init__(self) -> None:
        self._assessments: Dict[str, RiskAssessment] = {}
        self._vendor_index: Dict[str, list[str]] = {}
        self._lock = RLock()

    def submit(self, assessment: RiskAssessment) -> RiskAssessment:
        if not isinstance(assessment, RiskAssessment):
            raise ValidationError("assessment must be RiskAssessment")
        with self._lock:
            existing = self._assessments.get(assessment.assessment_id)
            if existing is not None:
                if existing == assessment:
                    return existing
                raise StorageConflictError("assessment id already exists")
            for assessment_id in self._vendor_index.get(assessment.vendor_id, []):
                other = self._assessments[assessment_id]
                if other.status == ReviewStatus.OPEN:
                    raise ValidationError("vendor already has an open assessment")
            self._assessments[assessment.assessment_id] = assessment
            self._vendor_index.setdefault(assessment.vendor_id, []).append(assessment.assessment_id)
            return assessment

    def decide(
        self,
        assessment_id: str,
        *,
        status: ReviewStatus,
        notes: str,
        expected_version: int,
    ) -> RiskAssessment:
        target = ReviewStatus(status)
        if target not in {ReviewStatus.APPROVED, ReviewStatus.REMEDIATION, ReviewStatus.REJECTED}:
            raise ValidationError("assessment decision is invalid")
        with self._lock:
            current = self._require(assessment_id)
            if current.version != expected_version:
                raise StorageConflictError("assessment version does not match")
            if current.status != ReviewStatus.OPEN:
                raise ValidationError("assessment has already been decided")
            if target == ReviewStatus.APPROVED and current.open_findings:
                serious = tuple(
                    item
                    for item in current.open_findings
                    if item.severity in {Criticality.HIGH, Criticality.CRITICAL}
                )
                if serious:
                    raise ValidationError("high-severity findings prevent approval")
            changed = replace(
                current,
                status=target,
                notes=required(notes, "decision notes", 2000),
                version=current.version + 1,
            )
            self._assessments[assessment_id] = changed
            return changed

    def resolve_finding(
        self,
        assessment_id: str,
        finding_id: str,
        *,
        resolved_at: datetime,
        expected_version: int,
    ) -> RiskAssessment:
        instant = utc(resolved_at, "finding resolution time")
        with self._lock:
            current = self._require(assessment_id)
            if current.version != expected_version:
                raise StorageConflictError("assessment version does not match")
            found = False
            findings: list[RiskFinding] = []
            for finding in current.findings:
                if finding.finding_id != finding_id:
                    findings.append(finding)
                    continue
                found = True
                if finding.resolved_at is not None:
                    raise ValidationError("finding is already resolved")
                findings.append(replace(finding, resolved_at=instant))
            if not found:
                raise NotFoundError("finding does not exist")
            changed = replace(current, findings=tuple(findings), version=current.version + 1)
            self._assessments[assessment_id] = changed
            return changed

    def expire(self, *, as_of: date) -> Tuple[RiskAssessment, ...]:
        changed: list[RiskAssessment] = []
        with self._lock:
            for assessment_id, current in tuple(self._assessments.items()):
                if current.valid_until <= as_of and current.status not in {ReviewStatus.EXPIRED, ReviewStatus.REJECTED}:
                    expired = replace(current, status=ReviewStatus.EXPIRED, version=current.version + 1)
                    self._assessments[assessment_id] = expired
                    changed.append(expired)
        return tuple(changed)

    def latest(self, vendor_id: str) -> RiskAssessment:
        with self._lock:
            ids = self._vendor_index.get(required(vendor_id, "vendor id"), [])
            if not ids:
                raise NotFoundError("vendor has no risk assessment")
            return max((self._assessments[item] for item in ids), key=lambda item: (item.assessed_on, item.assessment_id))

    def due_for_review(self, *, as_of: date, within_days: int = 30) -> Tuple[RiskAssessment, ...]:
        if within_days < 0:
            raise ValidationError("review window cannot be negative")
        with self._lock:
            latest = tuple(self.latest(vendor_id) for vendor_id in self._vendor_index)
        return tuple(
            sorted(
                (item for item in latest if (item.valid_until - as_of).days <= within_days),
                key=lambda item: (item.valid_until, item.vendor_id),
            )
        )

    def overdue_findings(self, *, as_of: date) -> Tuple[Tuple[str, RiskFinding], ...]:
        result: list[Tuple[str, RiskFinding]] = []
        with self._lock:
            for assessment in self._assessments.values():
                for finding in assessment.open_findings:
                    if finding.due_on < as_of:
                        result.append((assessment.vendor_id, finding))
        return tuple(sorted(result, key=lambda item: (item[1].due_on, item[0], item[1].finding_id)))

    def portfolio(
        self,
        vendors: Iterable[Vendor],
        *,
        as_of: date,
        annual_spend: Optional[Mapping[str, Decimal]] = None,
        concentration_limit: Decimal = Decimal("0.4"),
    ) -> PortfolioRisk:
        records = tuple(vendors)
        if concentration_limit <= 0 or concentration_limit > 1:
            raise ValidationError("concentration limit must be greater than zero and no more than one")
        spend = {key: Decimal(str(value)) for key, value in (annual_spend or {}).items()}
        if any(value < 0 or not value.is_finite() for value in spend.values()):
            raise ValidationError("annual spend must contain finite non-negative values")
        weights = {vendor.vendor_id: spend.get(vendor.vendor_id, Decimal("1")) for vendor in records}
        total = sum(weights.values(), Decimal("0"))
        if records and total == 0:
            weights = {vendor.vendor_id: Decimal("1") for vendor in records}
            total = Decimal(len(records))
        country_totals: Dict[str, Decimal] = {}
        service_totals: Dict[str, Decimal] = {}
        for vendor in records:
            weight = weights[vendor.vendor_id]
            country_totals[vendor.country] = country_totals.get(vendor.country, Decimal("0")) + weight
            services = vendor.services or (vendor.service_name.lower(),)
            portion = weight / Decimal(len(services))
            for service in services:
                service_totals[service] = service_totals.get(service, Decimal("0")) + portion
        country_share = max(country_totals.values(), default=Decimal("0")) / total if total else Decimal("0")
        service_share = max(service_totals.values(), default=Decimal("0")) / total if total else Decimal("0")
        warnings: list[str] = []
        if country_share > concentration_limit:
            warnings.append("country concentration exceeds limit")
        if service_share > concentration_limit:
            warnings.append("service concentration exceeds limit")
        overdue_reviews = 0
        open_findings = 0
        for vendor in records:
            try:
                assessment = self.latest(vendor.vendor_id)
            except NotFoundError:
                overdue_reviews += 1
                continue
            if assessment.valid_until <= as_of or assessment.status in {ReviewStatus.EXPIRED, ReviewStatus.REJECTED}:
                overdue_reviews += 1
            open_findings += len(assessment.open_findings)
        return PortfolioRisk(
            vendor_count=len(records),
            critical_vendor_count=sum(item.criticality == Criticality.CRITICAL for item in records),
            overdue_review_count=overdue_reviews,
            open_finding_count=open_findings,
            highest_country_share=country_share,
            highest_service_share=service_share,
            concentration_warnings=tuple(warnings),
        )

    def history(self, vendor_id: str) -> Tuple[RiskAssessment, ...]:
        with self._lock:
            values = tuple(self._assessments[item] for item in self._vendor_index.get(vendor_id, []))
        return tuple(sorted(values, key=lambda item: (item.assessed_on, item.assessment_id)))

    def _require(self, assessment_id: str) -> RiskAssessment:
        try:
            return self._assessments[required(assessment_id, "assessment id")]
        except KeyError as exc:
            raise NotFoundError("assessment does not exist") from exc
