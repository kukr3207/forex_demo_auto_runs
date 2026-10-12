"""Operational incident lifecycle, timeline, ownership, and postmortems."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, Mapping, Optional, Sequence, Tuple

from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError
from forex_monitor.models import ensure_utc


class IncidentSeverity(str, Enum):
    SEV1 = "sev1"
    SEV2 = "sev2"
    SEV3 = "sev3"
    SEV4 = "sev4"


class IncidentState(str, Enum):
    DETECTED = "detected"
    INVESTIGATING = "investigating"
    MITIGATING = "mitigating"
    MONITORING = "monitoring"
    RESOLVED = "resolved"
    CLOSED = "closed"


@dataclass(frozen=True)
class TimelineEntry:
    occurred_at: datetime
    actor: str
    kind: str
    message: str

    def __post_init__(self) -> None:
        if not self.actor.strip() or not self.kind.strip() or not self.message.strip():
            raise ValidationError("incident timeline fields cannot be blank")
        object.__setattr__(self, "occurred_at", ensure_utc(self.occurred_at, "timeline time"))


@dataclass(frozen=True)
class Incident:
    incident_id: str
    title: str
    severity: IncidentSeverity
    state: IncidentState
    detected_at: datetime
    services: Tuple[str, ...]
    commander: Optional[str] = None
    summary: str = ""
    resolved_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    timeline: Tuple[TimelineEntry, ...] = ()
    version: int = 1

    def __post_init__(self) -> None:
        if not self.incident_id.strip() or not self.title.strip():
            raise ValidationError("incident id and title cannot be blank")
        detected = ensure_utc(self.detected_at, "incident detection time")
        services = tuple(sorted({service.strip() for service in self.services if service.strip()}))
        if not services:
            raise ValidationError("incident requires at least one affected service")
        timeline = tuple(sorted(self.timeline, key=lambda item: item.occurred_at))
        if timeline and timeline[0].occurred_at < detected:
            raise ValidationError("incident timeline cannot precede detection")
        resolved = ensure_utc(self.resolved_at, "incident resolution time") if self.resolved_at else None
        closed = ensure_utc(self.closed_at, "incident close time") if self.closed_at else None
        if resolved and resolved < detected:
            raise ValidationError("incident resolution cannot precede detection")
        if closed and (not resolved or closed < resolved):
            raise ValidationError("incident close must follow resolution")
        object.__setattr__(self, "detected_at", detected)
        object.__setattr__(self, "services", services)
        object.__setattr__(self, "timeline", timeline)
        object.__setattr__(self, "severity", self.severity if isinstance(self.severity, IncidentSeverity) else IncidentSeverity(self.severity))
        object.__setattr__(self, "state", self.state if isinstance(self.state, IncidentState) else IncidentState(self.state))
        object.__setattr__(self, "resolved_at", resolved)
        object.__setattr__(self, "closed_at", closed)

    @property
    def time_to_resolution(self) -> Optional[timedelta]:
        return self.resolved_at - self.detected_at if self.resolved_at else None


TRANSITIONS = {
    IncidentState.DETECTED: {IncidentState.INVESTIGATING},
    IncidentState.INVESTIGATING: {IncidentState.MITIGATING, IncidentState.MONITORING, IncidentState.RESOLVED},
    IncidentState.MITIGATING: {IncidentState.INVESTIGATING, IncidentState.MONITORING, IncidentState.RESOLVED},
    IncidentState.MONITORING: {IncidentState.INVESTIGATING, IncidentState.MITIGATING, IncidentState.RESOLVED},
    IncidentState.RESOLVED: {IncidentState.CLOSED, IncidentState.INVESTIGATING},
}


class IncidentManager:
    def __init__(self) -> None:
        self._incidents: Dict[str, Incident] = {}

    def create(self, incident: Incident) -> Incident:
        existing = self._incidents.get(incident.incident_id)
        if existing is not None:
            if existing == incident:
                return existing
            raise StorageConflictError("incident id already exists")
        self._incidents[incident.incident_id] = incident
        return incident

    def get(self, incident_id: str) -> Incident:
        try:
            return self._incidents[incident_id]
        except KeyError as exc:
            raise NotFoundError("incident was not found") from exc

    def update(
        self,
        incident_id: str,
        state: IncidentState,
        at: datetime,
        actor: str,
        message: str,
        *,
        commander: Optional[str] = None,
        expected_version: Optional[int] = None,
    ) -> Incident:
        current = self.get(incident_id)
        target = state if isinstance(state, IncidentState) else IncidentState(state)
        if expected_version is not None and current.version != expected_version:
            raise StorageConflictError("incident version changed")
        if target not in TRANSITIONS.get(current.state, set()):
            raise ValidationError("incident state transition is not allowed")
        timestamp = ensure_utc(at, "incident update time")
        if current.timeline and timestamp < current.timeline[-1].occurred_at:
            raise ValidationError("incident update time cannot move backward")
        resolved = timestamp if target is IncidentState.RESOLVED else (
            None if current.state is IncidentState.RESOLVED and target is IncidentState.INVESTIGATING else current.resolved_at
        )
        closed = timestamp if target is IncidentState.CLOSED else current.closed_at
        entry = TimelineEntry(timestamp, actor, "state_change", message)
        changed = replace(
            current, state=target, commander=commander or current.commander,
            resolved_at=resolved, closed_at=closed,
            timeline=current.timeline + (entry,), version=current.version + 1,
        )
        self._incidents[incident_id] = changed
        return changed

    def note(self, incident_id: str, entry: TimelineEntry) -> Incident:
        current = self.get(incident_id)
        if entry.occurred_at < current.detected_at or (current.timeline and entry.occurred_at < current.timeline[-1].occurred_at):
            raise ValidationError("incident note time cannot move backward")
        changed = replace(current, timeline=current.timeline + (entry,), version=current.version + 1)
        self._incidents[incident_id] = changed
        return changed

    def open(self, service: Optional[str] = None) -> Tuple[Incident, ...]:
        return tuple(sorted(
            (
                item for item in self._incidents.values()
                if item.state is not IncidentState.CLOSED and (service is None or service in item.services)
            ),
            key=lambda item: (item.severity.value, item.detected_at, item.incident_id),
        ))

    def metrics(self) -> Mapping[str, object]:
        resolved = [item for item in self._incidents.values() if item.time_to_resolution is not None]
        mean_seconds = sum((item.time_to_resolution.total_seconds() for item in resolved), 0.0) / len(resolved) if resolved else None
        return {
            "total": len(self._incidents),
            "open": len(self.open()),
            "resolved": len(resolved),
            "meanTimeToResolutionSeconds": mean_seconds,
            "bySeverity": {
                severity.value: sum(1 for item in self._incidents.values() if item.severity is severity)
                for severity in IncidentSeverity
            },
        }
