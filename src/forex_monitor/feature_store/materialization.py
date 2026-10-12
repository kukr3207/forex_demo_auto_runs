"""Atomic batch materialization into offline and online feature stores."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from threading import RLock
from typing import Dict, Iterable, Mapping, Optional, Tuple

from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError
from forex_monitor.feature_store.models import FeatureValue, MaterializationRun, MaterializationState, utc
from forex_monitor.feature_store.registry import FeatureRegistry


class FeatureMaterializer:
    def __init__(self, registry: FeatureRegistry) -> None:
        self.registry = registry
        self._runs: Dict[str, MaterializationRun] = {}
        self._offline: Dict[Tuple[str, Tuple[Tuple[str, str], ...]], list[FeatureValue]] = {}
        self._online: Dict[Tuple[str, Tuple[Tuple[str, str], ...]], FeatureValue] = {}
        self._sources: Dict[Tuple[str, str], FeatureValue] = {}
        self._lock = RLock()

    def start(self, run: MaterializationRun) -> MaterializationRun:
        with self._lock:
            existing = self._runs.get(run.run_id)
            if existing is not None:
                if existing == run:
                    return existing
                raise StorageConflictError("materialization run id already exists")
            self.registry.dependency_order(run.feature_ids)
            changed = replace(run, state=MaterializationState.RUNNING, version=run.version + 1)
            self._runs[run.run_id] = changed
            return changed

    def write(self, run_id: str, values: Iterable[FeatureValue]) -> Tuple[int, int]:
        candidates = tuple(values)
        with self._lock:
            run = self._require_run(run_id)
            if run.state != MaterializationState.RUNNING:
                raise ValidationError("materialization run is not active")
            staged = []
            rejected = 0
            for value in candidates:
                if value.feature_id not in run.feature_ids or not (run.start_time <= value.event_time < run.end_time):
                    rejected += 1
                    continue
                feature = self.registry.require(value.feature_id)
                entity = self.registry.entity(feature.entity_id)
                if tuple(key for key, _ in value.entity_key) != tuple(sorted(entity.join_keys)):
                    rejected += 1
                    continue
                try:
                    validated = feature.validate_value(value.value)
                except ValidationError:
                    rejected += 1
                    continue
                normalized = replace(value, value=validated)
                source_key = (value.feature_id, value.source_reference)
                existing = self._sources.get(source_key)
                if existing is not None:
                    if existing != normalized:
                        raise StorageConflictError("feature source reference has different value")
                    continue
                staged.append(normalized)
            for value in staged:
                key = (value.feature_id, value.entity_key)
                history = self._offline.setdefault(key, [])
                history.append(value)
                history.sort(key=lambda item: (item.event_time, item.created_at, item.source_reference))
                feature = self.registry.require(value.feature_id)
                current = self._online.get(key)
                if feature.online and (current is None or value.event_time >= current.event_time):
                    self._online[key] = value
                self._sources[(value.feature_id, value.source_reference)] = value
            changed = replace(run, processed=run.processed + len(staged), rejected=run.rejected + rejected, version=run.version + 1)
            self._runs[run_id] = changed
            return len(staged), rejected

    def complete(self, run_id: str, *, completed_at: datetime, expected_version: int) -> MaterializationRun:
        instant = utc(completed_at, "completed at")
        with self._lock:
            current = self._require_run(run_id)
            if current.version != expected_version:
                raise StorageConflictError("materialization run version does not match")
            if current.state != MaterializationState.RUNNING:
                raise ValidationError("materialization run is not active")
            state = MaterializationState.PARTIAL if current.rejected else MaterializationState.COMPLETED
            changed = replace(current, state=state, completed_at=instant, version=current.version + 1)
            self._runs[current.run_id] = changed
            return changed

    def offline_values(self, feature_id: str, entity_key: Mapping[str, object], *, start: Optional[datetime] = None, end: Optional[datetime] = None) -> Tuple[FeatureValue, ...]:
        key = (feature_id, tuple(sorted((name, str(value)) for name, value in entity_key.items())))
        with self._lock:
            values = self._offline.get(key, ())
            return tuple(item for item in values if (start is None or item.event_time >= start) and (end is None or item.event_time < end))

    def online_value(self, feature_id: str, entity_key: Mapping[str, object]) -> Optional[FeatureValue]:
        key = (feature_id, tuple(sorted((name, str(value)) for name, value in entity_key.items())))
        with self._lock:
            return self._online.get(key)

    def _require_run(self, run_id: str) -> MaterializationRun:
        value = self._runs.get(run_id.strip())
        if value is None:
            raise NotFoundError("materialization run does not exist")
        return value
