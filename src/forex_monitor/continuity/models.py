"""Business-continuity records for backup and failover control."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional, Tuple

from forex_monitor.errors import ValidationError


def text(value: object, name: str, maximum: Optional[int] = None) -> str:
    result = str(value).strip() if value is not None else ""
    if not result:
        raise ValidationError("%s is required" % name)
    if maximum is not None and len(result) > maximum:
        raise ValidationError("%s exceeds maximum length" % name)
    return result


def utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError("%s must be timezone-aware" % name)
    return value.astimezone(timezone.utc)


class BackupType(str, Enum):
    FULL = "full"
    INCREMENTAL = "incremental"
    LOG = "log"
    SNAPSHOT = "snapshot"


class BackupState(str, Enum):
    CREATING = "creating"
    AVAILABLE = "available"
    CORRUPT = "corrupt"
    EXPIRED = "expired"
    DELETED = "deleted"


class RecoveryState(str, Enum):
    PLANNED = "planned"
    VALIDATING = "validating"
    RESTORING = "restoring"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class FailoverState(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILOVER_PENDING = "failover_pending"
    FAILING_OVER = "failing_over"
    FAILED_OVER = "failed_over"
    FAILBACK_PENDING = "failback_pending"
    FAILING_BACK = "failing_back"


@dataclass(frozen=True)
class BackupArtifact:
    backup_id: str
    resource_id: str
    backup_type: BackupType
    state: BackupState
    started_at: datetime
    completed_at: Optional[datetime]
    storage_uri: str
    checksum: str
    size_bytes: int
    sequence_start: Optional[int] = None
    sequence_end: Optional[int] = None
    base_backup_id: Optional[str] = None
    encrypted: bool = True
    key_id: Optional[str] = None
    expires_at: Optional[datetime] = None
    labels: Tuple[Tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "backup_id", text(self.backup_id, "backup id"))
        object.__setattr__(self, "resource_id", text(self.resource_id, "resource id"))
        object.__setattr__(self, "backup_type", BackupType(self.backup_type))
        object.__setattr__(self, "state", BackupState(self.state))
        started = utc(self.started_at, "started at")
        object.__setattr__(self, "started_at", started)
        if self.completed_at is not None:
            completed = utc(self.completed_at, "completed at")
            if completed < started:
                raise ValidationError("backup completion precedes start")
            object.__setattr__(self, "completed_at", completed)
        object.__setattr__(self, "storage_uri", text(self.storage_uri, "storage URI", 1000))
        digest = text(self.checksum, "checksum").lower()
        if len(digest) != 64 or any(item not in "0123456789abcdef" for item in digest):
            raise ValidationError("backup checksum must be SHA-256")
        object.__setattr__(self, "checksum", digest)
        if self.size_bytes < 0:
            raise ValidationError("backup size cannot be negative")
        if (self.sequence_start is None) != (self.sequence_end is None):
            raise ValidationError("backup sequence range is incomplete")
        if self.sequence_start is not None and self.sequence_end < self.sequence_start:
            raise ValidationError("backup sequence range is invalid")
        if self.backup_type in {BackupType.INCREMENTAL, BackupType.LOG} and not self.base_backup_id:
            raise ValidationError("incremental backup requires a base")
        if self.encrypted and not self.key_id:
            raise ValidationError("encrypted backup requires key id")
        if self.expires_at is not None:
            object.__setattr__(self, "expires_at", utc(self.expires_at, "expires at"))


@dataclass(frozen=True)
class RecoveryObjective:
    resource_id: str
    recovery_point: timedelta
    recovery_time: timedelta
    minimum_regions: int = 2
    maximum_data_loss_events: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "resource_id", text(self.resource_id, "resource id"))
        if self.recovery_point <= timedelta(0) or self.recovery_time <= timedelta(0):
            raise ValidationError("recovery objectives must be positive")
        if self.minimum_regions < 1:
            raise ValidationError("minimum regions must be positive")


@dataclass(frozen=True)
class RestorePlan:
    plan_id: str
    resource_id: str
    target_time: datetime
    backup_ids: Tuple[str, ...]
    target_environment: str
    created_at: datetime
    created_by: str
    estimated_size_bytes: int
    state: RecoveryState = RecoveryState.PLANNED
    validation_errors: Tuple[str, ...] = ()
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    failure_reason: Optional[str] = None
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_id", text(self.plan_id, "restore plan id"))
        object.__setattr__(self, "resource_id", text(self.resource_id, "resource id"))
        object.__setattr__(self, "target_time", utc(self.target_time, "target time"))
        if not self.backup_ids or len(self.backup_ids) != len(set(self.backup_ids)):
            raise ValidationError("restore plan backup chain is empty or duplicated")
        object.__setattr__(self, "target_environment", text(self.target_environment, "target environment"))
        object.__setattr__(self, "created_at", utc(self.created_at, "created at"))
        object.__setattr__(self, "created_by", text(self.created_by, "created by"))
        if self.estimated_size_bytes < 0:
            raise ValidationError("restore size cannot be negative")
        object.__setattr__(self, "state", RecoveryState(self.state))


@dataclass(frozen=True)
class RegionStatus:
    region: str
    healthy: bool
    observed_at: datetime
    lag_seconds: int
    writable: bool
    message: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "region", text(self.region, "region"))
        object.__setattr__(self, "observed_at", utc(self.observed_at, "observed at"))
        if self.lag_seconds < 0:
            raise ValidationError("region lag cannot be negative")


@dataclass(frozen=True)
class FailoverGroup:
    group_id: str
    resource_id: str
    primary_region: str
    secondary_regions: Tuple[str, ...]
    state: FailoverState = FailoverState.HEALTHY
    active_region: Optional[str] = None
    automatic: bool = False
    maximum_lag_seconds: int = 30
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "group_id", text(self.group_id, "failover group id"))
        object.__setattr__(self, "resource_id", text(self.resource_id, "resource id"))
        primary = text(self.primary_region, "primary region")
        secondaries = tuple(sorted(set(self.secondary_regions)))
        if not secondaries or primary in secondaries:
            raise ValidationError("failover regions are invalid")
        object.__setattr__(self, "primary_region", primary)
        object.__setattr__(self, "secondary_regions", secondaries)
        object.__setattr__(self, "state", FailoverState(self.state))
        object.__setattr__(self, "active_region", self.active_region or primary)
        if self.maximum_lag_seconds < 0:
            raise ValidationError("maximum failover lag cannot be negative")


@dataclass(frozen=True)
class RecoveryDrill:
    drill_id: str
    resource_id: str
    started_at: datetime
    completed_at: datetime
    restored_to: datetime
    data_loss_seconds: int
    recovery_seconds: int
    checks: Tuple[Tuple[str, bool], ...]
    notes: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "drill_id", text(self.drill_id, "drill id"))
        object.__setattr__(self, "resource_id", text(self.resource_id, "resource id"))
        started = utc(self.started_at, "started at")
        completed = utc(self.completed_at, "completed at")
        if completed < started:
            raise ValidationError("drill completion precedes start")
        object.__setattr__(self, "started_at", started)
        object.__setattr__(self, "completed_at", completed)
        object.__setattr__(self, "restored_to", utc(self.restored_to, "restored to"))
        if self.data_loss_seconds < 0 or self.recovery_seconds < 0:
            raise ValidationError("drill durations cannot be negative")

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(value for _, value in self.checks)
