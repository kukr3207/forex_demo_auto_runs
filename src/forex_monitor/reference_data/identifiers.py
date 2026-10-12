"""Temporal identifier assignments with collision and primary-key controls."""

from __future__ import annotations

from datetime import date
from threading import RLock
from typing import Dict, Iterable, Optional, Tuple

from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError
from forex_monitor.reference_data.models import IdentifierAssignment, IdentifierType, required


class IdentifierRegistry:
    def __init__(self) -> None:
        self._assignments: Dict[Tuple[IdentifierType, str, date], IdentifierAssignment] = {}
        self._lock = RLock()

    def assign(self, assignment: IdentifierAssignment) -> IdentifierAssignment:
        if not isinstance(assignment, IdentifierAssignment):
            raise ValidationError("assignment must be IdentifierAssignment")
        key = (assignment.identifier_type, assignment.value, assignment.valid_from)
        with self._lock:
            existing = self._assignments.get(key)
            if existing is not None:
                if existing == assignment:
                    return existing
                raise StorageConflictError("identifier assignment already exists")
            for item in self._assignments.values():
                if item.identifier_type == assignment.identifier_type and item.value == assignment.value and self._overlap(item, assignment):
                    if item.entity_type != assignment.entity_type or item.entity_id != assignment.entity_id:
                        raise StorageConflictError("identifier is assigned to another entity")
                if assignment.primary and item.primary and item.identifier_type == assignment.identifier_type and item.entity_type == assignment.entity_type and item.entity_id == assignment.entity_id and self._overlap(item, assignment):
                    raise StorageConflictError("entity already has a primary identifier of this type")
            self._assignments[key] = assignment
            return assignment

    def resolve(self, identifier_type: IdentifierType, value: str, *, on: date) -> IdentifierAssignment:
        kind = IdentifierType(identifier_type)
        normalized = required(value, "identifier value").upper()
        with self._lock:
            matches = [item for item in self._assignments.values() if item.identifier_type == kind and item.value == normalized and item.valid_on(on)]
            if not matches:
                raise NotFoundError("identifier is not assigned on requested date")
            if len(matches) > 1:
                raise StorageConflictError("identifier has overlapping assignments")
            return matches[0]

    def primary(self, entity_type: str, entity_id: str, identifier_type: IdentifierType, *, on: date) -> IdentifierAssignment:
        with self._lock:
            matches = [item for item in self._assignments.values() if item.entity_type == entity_type.lower() and item.entity_id == entity_id and item.identifier_type == IdentifierType(identifier_type) and item.primary and item.valid_on(on)]
            if not matches:
                raise NotFoundError("entity has no primary identifier of requested type")
            if len(matches) > 1:
                raise StorageConflictError("entity has overlapping primary identifiers")
            return matches[0]

    def identifiers(self, entity_type: str, entity_id: str, *, on: Optional[date] = None) -> Tuple[IdentifierAssignment, ...]:
        with self._lock:
            values = [item for item in self._assignments.values() if item.entity_type == entity_type.lower() and item.entity_id == entity_id and (on is None or item.valid_on(on))]
            return tuple(sorted(values, key=lambda item: (item.identifier_type.value, item.valid_from, item.value)))

    @staticmethod
    def _overlap(left: IdentifierAssignment, right: IdentifierAssignment) -> bool:
        return (left.valid_until is None or right.valid_from <= left.valid_until) and (right.valid_until is None or left.valid_from <= right.valid_until)
