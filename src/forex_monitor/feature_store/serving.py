"""Low-latency feature vectors with freshness and point-in-time joins."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from forex_monitor.errors import ValidationError
from forex_monitor.feature_store.materialization import FeatureMaterializer
from forex_monitor.feature_store.models import FeatureValue, utc
from forex_monitor.feature_store.registry import FeatureRegistry


@dataclass(frozen=True)
class ServedFeature:
    feature_id: str
    value: Any
    event_time: datetime
    stale: bool


@dataclass(frozen=True)
class FeatureVector:
    entity_key: Tuple[Tuple[str, str], ...]
    features: Tuple[ServedFeature, ...]
    as_of: datetime
    missing: Tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.missing

    def as_dict(self) -> Dict[str, Any]:
        return {item.feature_id: item.value for item in self.features}


class FeatureService:
    def __init__(self, registry: FeatureRegistry, materializer: FeatureMaterializer) -> None:
        self.registry = registry
        self.materializer = materializer

    def online(self, feature_ids: Iterable[str], entity_key: Mapping[str, object], *, at: datetime, require_fresh: bool = False) -> FeatureVector:
        instant = utc(at, "serve time")
        values = []
        missing = []
        for feature_id in self.registry.dependency_order(feature_ids):
            feature = self.registry.require(feature_id)
            value = self.materializer.online_value(feature_id, entity_key)
            if value is None or value.event_time > instant:
                missing.append(feature_id)
                continue
            stale = feature.ttl is not None and instant - value.event_time > feature.ttl
            if stale and require_fresh:
                missing.append(feature_id)
                continue
            values.append(ServedFeature(feature_id, value.value, value.event_time, stale))
        return FeatureVector(tuple(sorted((key, str(value)) for key, value in entity_key.items())), tuple(values), instant, tuple(missing))

    def historical(self, feature_ids: Iterable[str], entity_key: Mapping[str, object], *, as_of: datetime) -> FeatureVector:
        instant = utc(as_of, "as of")
        values = []
        missing = []
        for feature_id in self.registry.dependency_order(feature_ids):
            history = self.materializer.offline_values(feature_id, entity_key, end=instant)
            eligible = [item for item in history if item.event_time <= instant and item.created_at <= instant]
            if not eligible:
                missing.append(feature_id)
                continue
            selected = max(eligible, key=lambda item: (item.event_time, item.created_at, item.source_reference))
            feature = self.registry.require(feature_id)
            stale = feature.ttl is not None and instant - selected.event_time > feature.ttl
            values.append(ServedFeature(feature_id, selected.value, selected.event_time, stale))
        return FeatureVector(tuple(sorted((key, str(value)) for key, value in entity_key.items())), tuple(values), instant, tuple(missing))

    def training_set(self, feature_ids: Iterable[str], observations: Iterable[Tuple[Mapping[str, object], datetime]]) -> Tuple[FeatureVector, ...]:
        return tuple(self.historical(feature_ids, entity_key, as_of=as_of) for entity_key, as_of in observations)
