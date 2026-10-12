"""Core records for governed financial datasets."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Mapping, Optional, Tuple

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


def number(value: object, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError("%s must be numeric" % name) from exc
    if not result.is_finite():
        raise ValidationError("%s must be finite" % name)
    return result


class Classification(str, Enum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class FieldType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"
    DATE = "date"
    DATETIME = "datetime"
    OBJECT = "object"
    ARRAY = "array"


class CheckType(str, Enum):
    NOT_NULL = "not_null"
    UNIQUE = "unique"
    RANGE = "range"
    ALLOWED_VALUES = "allowed_values"
    REGEX = "regex"
    FRESHNESS = "freshness"
    ROW_COUNT = "row_count"


class CheckSeverity(str, Enum):
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class Dataset:
    dataset_id: str
    name: str
    domain: str
    owner: str
    description: str
    classification: Classification
    storage_uri: str
    created_at: datetime
    retention_days: int
    steward: Optional[str] = None
    tags: Tuple[str, ...] = ()
    active: bool = True
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "dataset_id", text(self.dataset_id, "dataset id", 100))
        object.__setattr__(self, "name", text(self.name, "dataset name", 200))
        object.__setattr__(self, "domain", text(self.domain, "data domain", 100).lower())
        object.__setattr__(self, "owner", text(self.owner, "dataset owner", 160))
        object.__setattr__(self, "description", text(self.description, "dataset description", 2000))
        object.__setattr__(self, "classification", Classification(self.classification))
        object.__setattr__(self, "storage_uri", text(self.storage_uri, "storage URI", 500))
        object.__setattr__(self, "created_at", utc(self.created_at, "created at"))
        if self.retention_days < 1 or self.retention_days > 36500:
            raise ValidationError("retention days are invalid")
        object.__setattr__(self, "steward", self.steward.strip() if self.steward else None)
        object.__setattr__(self, "tags", tuple(sorted(set(item.strip().lower() for item in self.tags if item.strip()))))
        if self.version < 1:
            raise ValidationError("dataset version must be positive")


@dataclass(frozen=True)
class SchemaField:
    name: str
    field_type: FieldType
    nullable: bool = False
    description: str = ""
    classification: Classification = Classification.INTERNAL
    format: Optional[str] = None
    default: Optional[Any] = None
    children: Tuple["SchemaField", ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", text(self.name, "field name", 100))
        object.__setattr__(self, "field_type", FieldType(self.field_type))
        object.__setattr__(self, "description", self.description.strip())
        object.__setattr__(self, "classification", Classification(self.classification))
        child_names = [item.name for item in self.children]
        if len(child_names) != len(set(child_names)):
            raise ValidationError("schema field has duplicate children")
        if self.children and self.field_type not in {FieldType.OBJECT, FieldType.ARRAY}:
            raise ValidationError("only object and array fields can contain children")


@dataclass(frozen=True)
class DatasetSchema:
    schema_id: str
    dataset_id: str
    version: int
    fields: Tuple[SchemaField, ...]
    created_at: datetime
    created_by: str
    compatibility: str = "backward"
    active: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_id", text(self.schema_id, "schema id"))
        object.__setattr__(self, "dataset_id", text(self.dataset_id, "dataset id"))
        if self.version < 1:
            raise ValidationError("schema version must be positive")
        names = [item.name for item in self.fields]
        if not names or len(names) != len(set(names)):
            raise ValidationError("schema fields are empty or duplicated")
        object.__setattr__(self, "created_at", utc(self.created_at, "created at"))
        object.__setattr__(self, "created_by", text(self.created_by, "schema creator"))
        if self.compatibility not in {"none", "backward", "forward", "full"}:
            raise ValidationError("schema compatibility mode is invalid")


@dataclass(frozen=True)
class QualityCheck:
    check_id: str
    dataset_id: str
    name: str
    check_type: CheckType
    field: Optional[str]
    severity: CheckSeverity
    parameters: Tuple[Tuple[str, str], ...] = ()
    threshold: Decimal = Decimal("1")
    enabled: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "check_id", text(self.check_id, "check id"))
        object.__setattr__(self, "dataset_id", text(self.dataset_id, "dataset id"))
        object.__setattr__(self, "name", text(self.name, "check name"))
        object.__setattr__(self, "check_type", CheckType(self.check_type))
        object.__setattr__(self, "field", self.field.strip() if self.field else None)
        object.__setattr__(self, "severity", CheckSeverity(self.severity))
        keys = [key for key, _ in self.parameters]
        if len(keys) != len(set(keys)):
            raise ValidationError("quality check parameters are duplicated")
        object.__setattr__(self, "parameters", tuple(sorted(self.parameters)))
        threshold = number(self.threshold, "quality threshold")
        if threshold < 0 or threshold > 1:
            raise ValidationError("quality threshold must be between zero and one")
        object.__setattr__(self, "threshold", threshold)


@dataclass(frozen=True)
class QualityResult:
    run_id: str
    check_id: str
    dataset_id: str
    checked_at: datetime
    passed: bool
    observed: Decimal
    expected: Decimal
    failed_rows: Tuple[int, ...] = ()
    message: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", text(self.run_id, "run id"))
        object.__setattr__(self, "check_id", text(self.check_id, "check id"))
        object.__setattr__(self, "dataset_id", text(self.dataset_id, "dataset id"))
        object.__setattr__(self, "checked_at", utc(self.checked_at, "checked at"))
        object.__setattr__(self, "observed", number(self.observed, "observed value"))
        object.__setattr__(self, "expected", number(self.expected, "expected value"))


@dataclass(frozen=True)
class LineageEdge:
    edge_id: str
    source_dataset_id: str
    target_dataset_id: str
    transformation: str
    job_id: str
    created_at: datetime
    field_mappings: Tuple[Tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "edge_id", text(self.edge_id, "edge id"))
        object.__setattr__(self, "source_dataset_id", text(self.source_dataset_id, "source dataset id"))
        object.__setattr__(self, "target_dataset_id", text(self.target_dataset_id, "target dataset id"))
        if self.source_dataset_id == self.target_dataset_id:
            raise ValidationError("lineage edge cannot reference one dataset twice")
        object.__setattr__(self, "transformation", text(self.transformation, "transformation", 2000))
        object.__setattr__(self, "job_id", text(self.job_id, "job id"))
        object.__setattr__(self, "created_at", utc(self.created_at, "created at"))


@dataclass(frozen=True)
class AccessPolicy:
    policy_id: str
    dataset_id: str
    principals: Tuple[str, ...]
    permissions: Tuple[str, ...]
    purpose: str
    expires_at: Optional[datetime] = None
    row_filter: Optional[str] = None
    masked_fields: Tuple[str, ...] = ()
    active: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "policy_id", text(self.policy_id, "policy id"))
        object.__setattr__(self, "dataset_id", text(self.dataset_id, "dataset id"))
        principals = tuple(sorted(set(item.strip() for item in self.principals if item.strip())))
        permissions = tuple(sorted(set(item.strip().lower() for item in self.permissions if item.strip())))
        if not principals or not permissions:
            raise ValidationError("access policy needs principals and permissions")
        if any(item not in {"read", "write", "delete", "export", "admin"} for item in permissions):
            raise ValidationError("access policy permission is invalid")
        object.__setattr__(self, "principals", principals)
        object.__setattr__(self, "permissions", permissions)
        object.__setattr__(self, "purpose", text(self.purpose, "access purpose", 500))
        if self.expires_at is not None:
            object.__setattr__(self, "expires_at", utc(self.expires_at, "expires at"))
        object.__setattr__(self, "masked_fields", tuple(sorted(set(self.masked_fields))))
