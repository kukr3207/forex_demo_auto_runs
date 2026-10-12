"""Vendor incident response with acknowledgement and resolution targets."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from threading import RLock
from typing import Dict, Mapping, Optional, Tuple

from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError
from forex_monitor.third_party.models import IncidentSeverity, IncidentStatus, VendorIncident, required, utc


@dataclass(frozen=True)
class IncidentSla:
    severity: IncidentSeverity
    acknowledge_within: timedelta
    resolve_within: timedelta
    update_every: timedelta

    def __post_init__(self) -> None:
        object.__setattr__(self, "severity", IncidentSeverity(self.severity))
        if self.acknowledge_within <= timedelta(0):
            raise ValidationError("acknowledgement target must be positive")
        if self.resolve_within <= self.acknowledge_within:
            raise ValidationError("resolution target must exceed acknowledgement target")
        if self.update_every <= timedelta(0):
            raise ValidationError("update cadence must be positive")


@dataclass(frozen=True)
class IncidentDeadline:
    incident_id: str
    acknowledge_by: datetime
    resolve_by: datetime
    next_update_by: datetime
    acknowledgement_breached: bool
    resolution_breached: bool
    update_breached: bool


class IncidentManager:
    def __init__(self, policies: Optional[Mapping[IncidentSeverity, IncidentSla]] = None) -> None:
        supplied = dict(policies or self.default_policies())
        normalized: Dict[IncidentSeverity, IncidentSla] = {}
        for severity in IncidentSeverity:
            policy = supplied.get(severity) or supplied.get(severity.value)  # type: ignore[arg-type]
            if policy is None or policy.severity != severity:
                raise ValidationError("incident policy is missing for %s" % severity.value)
            normalized[severity] = policy
        self._policies = normalized
        self._incidents: Dict[str, VendorIncident] = {}
        self._external: Dict[Tuple[str, str], str] = {}
        self._lock = RLock()

    @staticmethod
    def default_policies() -> Mapping[IncidentSeverity, IncidentSla]:
        return {
            IncidentSeverity.CRITICAL: IncidentSla(IncidentSeverity.CRITICAL, timedelta(minutes=15), timedelta(hours=4), timedelta(minutes=30)),
            IncidentSeverity.HIGH: IncidentSla(IncidentSeverity.HIGH, timedelta(minutes=30), timedelta(hours=8), timedelta(hours=1)),
            IncidentSeverity.MEDIUM: IncidentSla(IncidentSeverity.MEDIUM, timedelta(hours=2), timedelta(days=2), timedelta(hours=4)),
            IncidentSeverity.LOW: IncidentSla(IncidentSeverity.LOW, timedelta(hours=8), timedelta(days=5), timedelta(days=1)),
        }

    def open(self, incident: VendorIncident) -> VendorIncident:
        if not isinstance(incident, VendorIncident):
            raise ValidationError("incident must be VendorIncident")
        if incident.status != IncidentStatus.OPEN:
            raise ValidationError("new incident must be open")
        with self._lock:
            existing = self._incidents.get(incident.incident_id)
            if existing is not None:
                if existing == incident:
                    return existing
                raise StorageConflictError("incident id already exists")
            if incident.external_reference:
                key = (incident.vendor_id, incident.external_reference)
                duplicate_id = self._external.get(key)
                if duplicate_id:
                    duplicate = self._incidents[duplicate_id]
                    if duplicate == incident:
                        return duplicate
                    raise StorageConflictError("external incident reference already exists")
                self._external[key] = incident.incident_id
            self._incidents[incident.incident_id] = incident
            return incident

    def acknowledge(self, incident_id: str, *, at: datetime, expected_version: int) -> VendorIncident:
        instant = utc(at, "acknowledgement time")
        with self._lock:
            current = self._require(incident_id)
            self._check_version(current, expected_version)
            if current.status != IncidentStatus.OPEN:
                raise ValidationError("only an open incident can be acknowledged")
            changed = replace(
                current,
                status=IncidentStatus.ACKNOWLEDGED,
                acknowledged_at=instant,
                version=current.version + 1,
            )
            self._incidents[incident_id] = changed
            return changed

    def add_update(
        self,
        incident_id: str,
        *,
        message: str,
        at: datetime,
        expected_version: int,
        mitigating: bool = False,
    ) -> VendorIncident:
        instant = utc(at, "update time")
        text = required(message, "incident update", 1000)
        with self._lock:
            current = self._require(incident_id)
            self._check_version(current, expected_version)
            if current.status in {IncidentStatus.RESOLVED, IncidentStatus.CLOSED}:
                raise ValidationError("resolved incident cannot receive an operational update")
            if current.updates and instant < current.updates[-1][0]:
                raise ValidationError("incident updates must be chronological")
            status = IncidentStatus.MITIGATING if mitigating else current.status
            changed = replace(
                current,
                status=status,
                mitigated_at=instant if mitigating and current.mitigated_at is None else current.mitigated_at,
                updates=current.updates + ((instant, text),),
                version=current.version + 1,
            )
            self._incidents[incident_id] = changed
            return changed

    def resolve(
        self,
        incident_id: str,
        *,
        resolution: str,
        at: datetime,
        expected_version: int,
    ) -> VendorIncident:
        instant = utc(at, "resolution time")
        message = required(resolution, "incident resolution", 1000)
        with self._lock:
            current = self._require(incident_id)
            self._check_version(current, expected_version)
            if current.status not in {IncidentStatus.ACKNOWLEDGED, IncidentStatus.MITIGATING}:
                raise ValidationError("incident must be acknowledged before resolution")
            if current.updates and instant < current.updates[-1][0]:
                raise ValidationError("resolution cannot precede the latest update")
            changed = replace(
                current,
                status=IncidentStatus.RESOLVED,
                resolved_at=instant,
                updates=current.updates + ((instant, message),),
                version=current.version + 1,
            )
            self._incidents[incident_id] = changed
            return changed

    def close(self, incident_id: str, *, expected_version: int) -> VendorIncident:
        with self._lock:
            current = self._require(incident_id)
            self._check_version(current, expected_version)
            if current.status != IncidentStatus.RESOLVED:
                raise ValidationError("only a resolved incident can be closed")
            changed = replace(current, status=IncidentStatus.CLOSED, version=current.version + 1)
            self._incidents[incident_id] = changed
            return changed

    def deadline(self, incident_id: str, *, as_of: datetime) -> IncidentDeadline:
        current = self.get(incident_id)
        instant = utc(as_of, "as of")
        policy = self._policies[current.severity]
        acknowledge_by = current.detected_at + policy.acknowledge_within
        resolve_by = current.detected_at + policy.resolve_within
        last_communication = current.updates[-1][0] if current.updates else current.detected_at
        next_update_by = last_communication + policy.update_every
        return IncidentDeadline(
            current.incident_id,
            acknowledge_by,
            resolve_by,
            next_update_by,
            current.acknowledged_at is None and instant > acknowledge_by,
            current.resolved_at is None and instant > resolve_by,
            current.status not in {IncidentStatus.RESOLVED, IncidentStatus.CLOSED} and instant > next_update_by,
        )

    def breaches(self, *, as_of: datetime) -> Tuple[IncidentDeadline, ...]:
        with self._lock:
            ids = tuple(self._incidents)
        result = tuple(self.deadline(incident_id, as_of=as_of) for incident_id in ids)
        return tuple(
            sorted(
                (item for item in result if item.acknowledgement_breached or item.resolution_breached or item.update_breached),
                key=lambda item: (item.resolve_by, item.incident_id),
            )
        )

    def get(self, incident_id: str) -> VendorIncident:
        with self._lock:
            return self._require(incident_id)

    def list(
        self,
        *,
        vendor_id: Optional[str] = None,
        status: Optional[IncidentStatus] = None,
        severity: Optional[IncidentSeverity] = None,
    ) -> Tuple[VendorIncident, ...]:
        with self._lock:
            values = tuple(self._incidents.values())
        if vendor_id is not None:
            values = tuple(item for item in values if item.vendor_id == vendor_id)
        if status is not None:
            target_status = IncidentStatus(status)
            values = tuple(item for item in values if item.status == target_status)
        if severity is not None:
            target_severity = IncidentSeverity(severity)
            values = tuple(item for item in values if item.severity == target_severity)
        return tuple(sorted(values, key=lambda item: (item.detected_at, item.incident_id)))

    def mean_time_to_acknowledge(self, *, vendor_id: Optional[str] = None) -> Optional[timedelta]:
        values = tuple(item for item in self.list(vendor_id=vendor_id) if item.acknowledged_at is not None)
        if not values:
            return None
        seconds = sum((item.acknowledged_at - item.detected_at).total_seconds() for item in values)
        return timedelta(seconds=seconds / len(values))

    def mean_time_to_resolve(self, *, vendor_id: Optional[str] = None) -> Optional[timedelta]:
        values = tuple(item for item in self.list(vendor_id=vendor_id) if item.resolved_at is not None)
        if not values:
            return None
        seconds = sum((item.resolved_at - item.detected_at).total_seconds() for item in values)
        return timedelta(seconds=seconds / len(values))

    def _require(self, incident_id: str) -> VendorIncident:
        try:
            return self._incidents[required(incident_id, "incident id")]
        except KeyError as exc:
            raise NotFoundError("incident does not exist") from exc

    @staticmethod
    def _check_version(current: VendorIncident, expected_version: int) -> None:
        if current.version != expected_version:
            raise StorageConflictError("incident version does not match")
