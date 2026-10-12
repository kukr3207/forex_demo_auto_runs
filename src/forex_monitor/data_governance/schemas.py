"""Schema evolution and row validation for catalogued datasets."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from threading import RLock
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from forex_monitor.data_governance.models import DatasetSchema, FieldType, SchemaField
from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError


class SchemaRegistry:
    def __init__(self) -> None:
        self._schemas: Dict[Tuple[str, int], DatasetSchema] = {}
        self._active: Dict[str, int] = {}
        self._lock = RLock()

    def register(self, schema: DatasetSchema) -> DatasetSchema:
        if not isinstance(schema, DatasetSchema):
            raise ValidationError("schema must be DatasetSchema")
        key = (schema.dataset_id, schema.version)
        with self._lock:
            existing = self._schemas.get(key)
            if existing is not None:
                if existing == schema:
                    return existing
                raise StorageConflictError("schema version already exists")
            latest = self.latest(schema.dataset_id, required=False)
            if latest is not None:
                if schema.version != latest.version + 1:
                    raise ValidationError("schema versions must be consecutive")
                self._validate_compatibility(latest, schema)
            elif schema.version != 1:
                raise ValidationError("first schema version must be one")
            self._schemas[key] = schema
            if schema.active:
                self._active[schema.dataset_id] = schema.version
            return schema

    def latest(self, dataset_id: str, *, required: bool = True) -> Optional[DatasetSchema]:
        versions = [version for item_id, version in self._schemas if item_id == dataset_id]
        if not versions:
            if required:
                raise NotFoundError("dataset schema does not exist")
            return None
        return self._schemas[(dataset_id, max(versions))]

    def active(self, dataset_id: str) -> DatasetSchema:
        version = self._active.get(dataset_id)
        if version is None:
            raise NotFoundError("dataset has no active schema")
        return self._schemas[(dataset_id, version)]

    def versions(self, dataset_id: str) -> Tuple[DatasetSchema, ...]:
        with self._lock:
            return tuple(self._schemas[(dataset_id, version)] for version in sorted(version for item_id, version in self._schemas if item_id == dataset_id))

    def validate_row(self, dataset_id: str, row: Mapping[str, Any], *, version: Optional[int] = None, reject_unknown: bool = True) -> Tuple[str, ...]:
        with self._lock:
            schema = self._schemas.get((dataset_id, version)) if version is not None else self.active(dataset_id)
            if schema is None:
                raise NotFoundError("dataset schema version does not exist")
        fields = {item.name: item for item in schema.fields}
        errors = []
        if reject_unknown:
            errors.extend("unknown field: %s" % key for key in row if key not in fields)
        for name, field in fields.items():
            value = row.get(name)
            if value is None:
                if not field.nullable and field.default is None:
                    errors.append("required field is missing: %s" % name)
                continue
            if not self._matches(field, value):
                errors.append("field has wrong type: %s" % name)
        return tuple(errors)

    def apply_defaults(self, dataset_id: str, row: Mapping[str, Any]) -> Dict[str, Any]:
        schema = self.active(dataset_id)
        result = dict(row)
        for field in schema.fields:
            if field.name not in result and field.default is not None:
                result[field.name] = field.default
        return result

    @staticmethod
    def _matches(field: SchemaField, value: Any) -> bool:
        if field.field_type == FieldType.STRING:
            return isinstance(value, str)
        if field.field_type == FieldType.INTEGER:
            return isinstance(value, int) and not isinstance(value, bool)
        if field.field_type == FieldType.DECIMAL:
            try:
                Decimal(str(value))
                return not isinstance(value, bool)
            except Exception:
                return False
        if field.field_type == FieldType.BOOLEAN:
            return isinstance(value, bool)
        if field.field_type == FieldType.DATE:
            return isinstance(value, date) and not isinstance(value, datetime)
        if field.field_type == FieldType.DATETIME:
            return isinstance(value, datetime) and value.tzinfo is not None
        if field.field_type == FieldType.OBJECT:
            return isinstance(value, Mapping)
        if field.field_type == FieldType.ARRAY:
            return isinstance(value, (list, tuple))
        return False

    @staticmethod
    def _validate_compatibility(previous: DatasetSchema, current: DatasetSchema) -> None:
        mode = current.compatibility
        if mode == "none":
            return
        old = {item.name: item for item in previous.fields}
        new = {item.name: item for item in current.fields}
        if mode in {"backward", "full"}:
            for name, field in old.items():
                replacement = new.get(name)
                if replacement is None:
                    raise ValidationError("backward-compatible schema cannot remove fields")
                if replacement.field_type != field.field_type:
                    raise ValidationError("backward-compatible schema cannot change field type")
            for name, field in new.items():
                if name not in old and not field.nullable and field.default is None:
                    raise ValidationError("new backward-compatible fields need default or nullable")
        if mode in {"forward", "full"}:
            for name, field in new.items():
                if name in old and old[name].field_type != field.field_type:
                    raise ValidationError("forward-compatible schema cannot change field type")
