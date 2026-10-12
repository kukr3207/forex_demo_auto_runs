"""Feature-store definitions and point-in-time values."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Optional, Tuple

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


class ValueType(str, Enum):
    DECIMAL = "decimal"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    STRING = "string"
    CATEGORY = "category"


class MaterializationState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass(frozen=True)
class EntityDefinition:
    entity_id: str
    name: str
    join_keys: Tuple[str, ...]
    description: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "entity_id", text(self.entity_id, "entity id"))
        object.__setattr__(self, "name", text(self.name, "entity name"))
        keys = tuple(item.strip() for item in self.join_keys if item.strip())
        if not keys or len(keys) != len(set(keys)):
            raise ValidationError("entity join keys are empty or duplicated")
        object.__setattr__(self, "join_keys", keys)


@dataclass(frozen=True)
class FeatureDefinition:
    feature_id: str
    name: str
    entity_id: str
    value_type: ValueType
    source: str
    expression: str
    owner: str
    created_at: datetime
    ttl: Optional[timedelta] = None
    description: str = ""
    categories: Tuple[str, ...] = ()
    minimum: Optional[Decimal] = None
    maximum: Optional[Decimal] = None
    online: bool = True
    active: bool = True
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "feature_id", text(self.feature_id, "feature id"))
        object.__setattr__(self, "name", text(self.name, "feature name"))
        object.__setattr__(self, "entity_id", text(self.entity_id, "entity id"))
        object.__setattr__(self, "value_type", ValueType(self.value_type))
        object.__setattr__(self, "source", text(self.source, "feature source"))
        object.__setattr__(self, "expression", text(self.expression, "feature expression", 4000))
        object.__setattr__(self, "owner", text(self.owner, "feature owner"))
        object.__setattr__(self, "created_at", utc(self.created_at, "created at"))
        if self.ttl is not None and self.ttl <= timedelta(0):
            raise ValidationError("feature TTL must be positive")
        categories = tuple(sorted(set(self.categories)))
        if self.value_type == ValueType.CATEGORY and not categories:
            raise ValidationError("categorical feature requires categories")
        object.__setattr__(self, "categories", categories)
        if self.minimum is not None:
            object.__setattr__(self, "minimum", Decimal(str(self.minimum)))
        if self.maximum is not None:
            object.__setattr__(self, "maximum", Decimal(str(self.maximum)))
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValidationError("feature minimum exceeds maximum")

    def validate_value(self, value: Any) -> Any:
        if value is None:
            raise ValidationError("feature value cannot be null")
        if self.value_type == ValueType.DECIMAL:
            try:
                result = Decimal(str(value))
            except (InvalidOperation, ValueError) as exc:
                raise ValidationError("feature value must be decimal") from exc
            if not result.is_finite():
                raise ValidationError("feature value must be finite")
            if self.minimum is not None and result < self.minimum:
                raise ValidationError("feature value is below minimum")
            if self.maximum is not None and result > self.maximum:
                raise ValidationError("feature value exceeds maximum")
            return result
        if self.value_type == ValueType.INTEGER:
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValidationError("feature value must be integer")
            return value
        if self.value_type == ValueType.BOOLEAN:
            if not isinstance(value, bool):
                raise ValidationError("feature value must be boolean")
            return value
        result = str(value)
        if self.value_type == ValueType.CATEGORY and result not in self.categories:
            raise ValidationError("feature value is outside categories")
        return result


@dataclass(frozen=True)
class FeatureValue:
    feature_id: str
    entity_key: Tuple[Tuple[str, str], ...]
    value: Any
    event_time: datetime
    created_at: datetime
    source_reference: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "feature_id", text(self.feature_id, "feature id"))
        keys = [key for key, _ in self.entity_key]
        if not keys or len(keys) != len(set(keys)):
            raise ValidationError("feature entity key is empty or duplicated")
        object.__setattr__(self, "entity_key", tuple(sorted((text(key, "entity key"), str(value)) for key, value in self.entity_key)))
        event = utc(self.event_time, "event time")
        created = utc(self.created_at, "created at")
        if event > created:
            raise ValidationError("feature event time cannot follow creation")
        object.__setattr__(self, "event_time", event)
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "source_reference", text(self.source_reference, "source reference"))


@dataclass(frozen=True)
class MaterializationRun:
    run_id: str
    feature_ids: Tuple[str, ...]
    start_time: datetime
    end_time: datetime
    requested_at: datetime
    state: MaterializationState = MaterializationState.PENDING
    processed: int = 0
    rejected: int = 0
    error: Optional[str] = None
    completed_at: Optional[datetime] = None
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", text(self.run_id, "run id"))
        features = tuple(sorted(set(self.feature_ids)))
        if not features:
            raise ValidationError("materialization requires features")
        object.__setattr__(self, "feature_ids", features)
        start = utc(self.start_time, "start time")
        end = utc(self.end_time, "end time")
        if end <= start:
            raise ValidationError("materialization end must follow start")
        object.__setattr__(self, "start_time", start)
        object.__setattr__(self, "end_time", end)
        object.__setattr__(self, "requested_at", utc(self.requested_at, "requested at"))
        object.__setattr__(self, "state", MaterializationState(self.state))
        if self.processed < 0 or self.rejected < 0:
            raise ValidationError("materialization counts cannot be negative")
