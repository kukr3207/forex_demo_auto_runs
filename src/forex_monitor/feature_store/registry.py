"""Versioned entity and feature registry with dependency checks."""

from __future__ import annotations

from dataclasses import replace
from threading import RLock
from typing import Dict, Iterable, Optional, Tuple

from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError
from forex_monitor.feature_store.models import EntityDefinition, FeatureDefinition, text


class FeatureRegistry:
    def __init__(self) -> None:
        self._entities: Dict[str, EntityDefinition] = {}
        self._features: Dict[str, FeatureDefinition] = {}
        self._names: Dict[Tuple[str, str], str] = {}
        self._dependencies: Dict[str, Tuple[str, ...]] = {}
        self._lock = RLock()

    def add_entity(self, entity: EntityDefinition) -> EntityDefinition:
        with self._lock:
            existing = self._entities.get(entity.entity_id)
            if existing is not None:
                if existing == entity:
                    return existing
                raise StorageConflictError("feature entity id already exists")
            if any(item.name == entity.name for item in self._entities.values()):
                raise StorageConflictError("feature entity name already exists")
            self._entities[entity.entity_id] = entity
            return entity

    def add_feature(self, feature: FeatureDefinition, *, dependencies: Iterable[str] = ()) -> FeatureDefinition:
        dependency_values = tuple(sorted(set(dependencies)))
        with self._lock:
            existing = self._features.get(feature.feature_id)
            if existing is not None:
                if existing == feature and self._dependencies[feature.feature_id] == dependency_values:
                    return existing
                raise StorageConflictError("feature id already exists")
            if feature.entity_id not in self._entities:
                raise ValidationError("feature entity does not exist")
            if any(item not in self._features for item in dependency_values):
                raise ValidationError("feature dependency does not exist")
            if any(self._features[item].entity_id != feature.entity_id for item in dependency_values):
                raise ValidationError("feature dependency uses another entity")
            name_key = (feature.entity_id, feature.name)
            if name_key in self._names:
                raise StorageConflictError("feature name already exists for entity")
            self._features[feature.feature_id] = feature
            self._dependencies[feature.feature_id] = dependency_values
            self._names[name_key] = feature.feature_id
            return feature

    def deactivate(self, feature_id: str, *, expected_version: int) -> FeatureDefinition:
        with self._lock:
            current = self.require(feature_id)
            if current.version != expected_version:
                raise StorageConflictError("feature version does not match")
            dependants = [item for item, dependencies in self._dependencies.items() if feature_id in dependencies and self._features[item].active]
            if dependants:
                raise ValidationError("active features depend on this feature")
            changed = replace(current, active=False, version=current.version + 1)
            self._features[current.feature_id] = changed
            return changed

    def require(self, feature_id: str) -> FeatureDefinition:
        value = self._features.get(text(feature_id, "feature id"))
        if value is None:
            raise NotFoundError("feature does not exist")
        return value

    def entity(self, entity_id: str) -> EntityDefinition:
        value = self._entities.get(text(entity_id, "entity id"))
        if value is None:
            raise NotFoundError("feature entity does not exist")
        return value

    def features(self, entity_id: Optional[str] = None, *, active_only: bool = True) -> Tuple[FeatureDefinition, ...]:
        with self._lock:
            values = [item for item in self._features.values() if (entity_id is None or item.entity_id == entity_id) and (not active_only or item.active)]
            return tuple(sorted(values, key=lambda item: (item.entity_id, item.name)))

    def dependency_order(self, feature_ids: Iterable[str]) -> Tuple[str, ...]:
        wanted = set(feature_ids)
        for feature_id in tuple(wanted):
            self.require(feature_id)
            self._collect(feature_id, wanted)
        result = []
        pending = set(wanted)
        while pending:
            ready = sorted(item for item in pending if set(self._dependencies[item]) <= set(result))
            if not ready:
                raise ValidationError("feature dependency graph contains a cycle")
            result.extend(ready)
            pending -= set(ready)
        return tuple(result)

    def _collect(self, feature_id: str, result: set[str]) -> None:
        for dependency in self._dependencies.get(feature_id, ()):
            if dependency not in result:
                result.add(dependency)
                self._collect(dependency, result)
