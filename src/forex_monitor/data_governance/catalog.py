"""Searchable dataset catalog with stewardship and retention controls."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from threading import RLock
from typing import Dict, Iterable, Optional, Tuple

from forex_monitor.data_governance.models import Classification, Dataset, text, utc
from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError


class DataCatalog:
    def __init__(self) -> None:
        self._datasets: Dict[str, Dataset] = {}
        self._name_index: Dict[Tuple[str, str], str] = {}
        self._lock = RLock()

    def register(self, dataset: Dataset) -> Dataset:
        if not isinstance(dataset, Dataset):
            raise ValidationError("dataset must be Dataset")
        with self._lock:
            existing = self._datasets.get(dataset.dataset_id)
            if existing is not None:
                if existing == dataset:
                    return existing
                raise StorageConflictError("dataset id already exists")
            key = (dataset.domain, dataset.name.lower())
            if key in self._name_index:
                raise StorageConflictError("dataset name already exists in domain")
            self._datasets[dataset.dataset_id] = dataset
            self._name_index[key] = dataset.dataset_id
            return dataset

    def update(
        self,
        dataset_id: str,
        *,
        expected_version: int,
        owner: Optional[str] = None,
        steward: Optional[str] = None,
        description: Optional[str] = None,
        classification: Optional[Classification] = None,
        retention_days: Optional[int] = None,
        tags: Optional[Iterable[str]] = None,
        active: Optional[bool] = None,
    ) -> Dataset:
        with self._lock:
            current = self.require(dataset_id)
            if current.version != expected_version:
                raise StorageConflictError("dataset version does not match")
            changes = {"version": current.version + 1}
            if owner is not None:
                changes["owner"] = owner
            if steward is not None:
                changes["steward"] = steward
            if description is not None:
                changes["description"] = description
            if classification is not None:
                changes["classification"] = classification
            if retention_days is not None:
                changes["retention_days"] = retention_days
            if tags is not None:
                changes["tags"] = tuple(tags)
            if active is not None:
                changes["active"] = active
            changed = replace(current, **changes)
            self._datasets[current.dataset_id] = changed
            return changed

    def require(self, dataset_id: str) -> Dataset:
        value = self._datasets.get(text(dataset_id, "dataset id"))
        if value is None:
            raise NotFoundError("dataset does not exist")
        return value

    def get(self, dataset_id: str) -> Optional[Dataset]:
        with self._lock:
            return self._datasets.get(dataset_id.strip())

    def search(
        self,
        query: str = "",
        *,
        domain: Optional[str] = None,
        owner: Optional[str] = None,
        classification: Optional[Classification] = None,
        tags: Iterable[str] = (),
        active_only: bool = True,
    ) -> Tuple[Dataset, ...]:
        needle = query.strip().lower()
        wanted_tags = set(item.lower() for item in tags)
        with self._lock:
            values = [
                item for item in self._datasets.values()
                if (not needle or needle in item.name.lower() or needle in item.description.lower())
                and (domain is None or item.domain == domain.lower())
                and (owner is None or item.owner == owner)
                and (classification is None or item.classification == classification)
                and wanted_tags.issubset(item.tags)
                and (not active_only or item.active)
            ]
            return tuple(sorted(values, key=lambda item: (item.domain, item.name.lower())))

    def retention_due(self, at: datetime) -> Tuple[Dataset, ...]:
        instant = utc(at, "retention time")
        with self._lock:
            values = [item for item in self._datasets.values() if item.active and item.created_at + timedelta(days=item.retention_days) <= instant]
            return tuple(sorted(values, key=lambda item: item.created_at))

    def domains(self) -> Tuple[str, ...]:
        with self._lock:
            return tuple(sorted(set(item.domain for item in self._datasets.values())))
