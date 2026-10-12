"""Immutable, versioned research datasets with lineage and quality metadata."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
import json
from typing import Dict, Iterable, Iterator, Mapping, Optional, Sequence, Tuple

from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError
from forex_monitor.models import as_decimal, ensure_utc, normalize_symbol


@dataclass(frozen=True, order=True)
class Observation:
    observed_at: datetime
    symbol: str
    fields: Mapping[str, Decimal]
    source: str

    def __post_init__(self) -> None:
        timestamp = ensure_utc(self.observed_at, "observation time")
        fields = {}
        for name, value in self.fields.items():
            normalized = name.strip()
            if not normalized:
                raise ValidationError("observation field name cannot be blank")
            fields[normalized] = as_decimal(value, normalized)
        if not fields:
            raise ValidationError("observation requires at least one field")
        source = self.source.strip()
        if not source:
            raise ValidationError("observation source cannot be blank")
        object.__setattr__(self, "observed_at", timestamp)
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(self, "fields", fields)
        object.__setattr__(self, "source", source)

    def canonical(self) -> Mapping[str, object]:
        return {
            "observedAt": self.observed_at.isoformat(),
            "symbol": self.symbol,
            "source": self.source,
            "fields": {name: str(value) for name, value in sorted(self.fields.items())},
        }


@dataclass(frozen=True)
class Dataset:
    dataset_id: str
    version: int
    observations: Tuple[Observation, ...]
    created_at: datetime
    description: str = ""
    parent_digest: Optional[str] = None
    tags: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.dataset_id.strip():
            raise ValidationError("dataset id cannot be blank")
        if self.version <= 0:
            raise ValidationError("dataset version must be positive")
        ordered = tuple(sorted(self.observations, key=lambda item: (item.observed_at, item.symbol, item.source)))
        if len({(item.observed_at, item.symbol, item.source) for item in ordered}) != len(ordered):
            raise ValidationError("dataset contains duplicate observation keys")
        object.__setattr__(self, "observations", ordered)
        object.__setattr__(self, "created_at", ensure_utc(self.created_at, "dataset creation time"))
        object.__setattr__(self, "tags", tuple(sorted({tag.strip().lower() for tag in self.tags if tag.strip()})))

    @property
    def digest(self) -> str:
        payload = {
            "datasetId": self.dataset_id,
            "version": self.version,
            "parentDigest": self.parent_digest,
            "observations": [item.canonical() for item in self.observations],
        }
        return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    @property
    def start(self) -> Optional[datetime]:
        return self.observations[0].observed_at if self.observations else None

    @property
    def end(self) -> Optional[datetime]:
        return self.observations[-1].observed_at if self.observations else None

    @property
    def symbols(self) -> Tuple[str, ...]:
        return tuple(sorted({item.symbol for item in self.observations}))

    def filter(
        self,
        *,
        symbols: Optional[Sequence[str]] = None,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> Tuple[Observation, ...]:
        selected = {normalize_symbol(symbol) for symbol in symbols} if symbols else None
        start_time = ensure_utc(start, "dataset start") if start else None
        end_time = ensure_utc(end, "dataset end") if end else None
        if start_time and end_time and end_time < start_time:
            raise ValidationError("dataset filter end cannot precede start")
        return tuple(
            item for item in self.observations
            if (selected is None or item.symbol in selected)
            and (start_time is None or item.observed_at >= start_time)
            and (end_time is None or item.observed_at <= end_time)
        )

    def field_coverage(self) -> Mapping[str, Decimal]:
        if not self.observations:
            return {}
        counts: Dict[str, int] = {}
        for item in self.observations:
            for field in item.fields:
                counts[field] = counts.get(field, 0) + 1
        total = Decimal(len(self.observations))
        return {name: Decimal(count) / total for name, count in sorted(counts.items())}


class DatasetCatalog:
    def __init__(self) -> None:
        self._datasets: Dict[Tuple[str, int], Dataset] = {}
        self._digests: Dict[str, Tuple[str, int]] = {}

    def publish(self, dataset: Dataset) -> Dataset:
        key = (dataset.dataset_id, dataset.version)
        existing = self._datasets.get(key)
        if existing is not None:
            if existing.digest == dataset.digest:
                return existing
            raise StorageConflictError("dataset version already exists with different content")
        if dataset.version > 1:
            prior = self._datasets.get((dataset.dataset_id, dataset.version - 1))
            if prior is None:
                raise ValidationError("dataset versions must be published consecutively")
            if dataset.parent_digest != prior.digest:
                raise ValidationError("dataset parent digest does not match the prior version")
        elif dataset.parent_digest is not None:
            raise ValidationError("first dataset version cannot have a parent digest")
        if dataset.digest in self._digests:
            raise StorageConflictError("identical dataset content is already published")
        self._datasets[key] = dataset
        self._digests[dataset.digest] = key
        return dataset

    def get(self, dataset_id: str, version: Optional[int] = None) -> Dataset:
        if version is None:
            versions = [candidate for candidate_id, candidate in self._datasets if candidate_id == dataset_id]
            if not versions:
                raise NotFoundError("dataset was not found")
            version = max(versions)
        try:
            return self._datasets[(dataset_id, version)]
        except KeyError as exc:
            raise NotFoundError("dataset version was not found") from exc

    def lineage(self, dataset_id: str) -> Tuple[Dataset, ...]:
        values = [item for (candidate, _), item in self._datasets.items() if candidate == dataset_id]
        return tuple(sorted(values, key=lambda item: item.version))
