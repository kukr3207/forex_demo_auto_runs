from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from forex_monitor.errors import StorageConflictError, ValidationError
from forex_monitor.feature_store.materialization import FeatureMaterializer
from forex_monitor.feature_store.models import EntityDefinition, FeatureDefinition, FeatureValue, MaterializationRun, MaterializationState, ValueType
from forex_monitor.feature_store.registry import FeatureRegistry
from forex_monitor.feature_store.serving import FeatureService


NOW = datetime(2026, 10, 12, 8, tzinfo=timezone.utc)


def setup_store():
    registry = FeatureRegistry()
    registry.add_entity(EntityDefinition("instrument", "Instrument", ("symbol",)))
    registry.add_feature(FeatureDefinition("return", "return_1h", "instrument", ValueType.DECIMAL, "quotes", "close / lag(close) - 1", "quant", NOW, ttl=timedelta(hours=2), minimum=Decimal("-1"), maximum=Decimal("1")))
    registry.add_feature(FeatureDefinition("signal", "signal", "instrument", ValueType.CATEGORY, "features", "case", "quant", NOW, categories=("buy", "hold", "sell")), dependencies=("return",))
    materializer = FeatureMaterializer(registry)
    return registry, materializer, FeatureService(registry, materializer)


def test_dependency_order_includes_prerequisites() -> None:
    registry, _, _ = setup_store()
    assert registry.dependency_order(("signal",)) == ("return", "signal")


def test_materialization_validates_and_serves_latest_value() -> None:
    registry, materializer, service = setup_store()
    run = materializer.start(MaterializationRun("run", ("return",), NOW - timedelta(days=1), NOW + timedelta(days=1), NOW))
    value = FeatureValue("return", (("symbol", "EURUSD"),), Decimal("0.01"), NOW, NOW, "source-1")
    processed, rejected = materializer.write(run.run_id, (value,))
    assert (processed, rejected) == (1, 0)
    vector = service.online(("return",), {"symbol": "EURUSD"}, at=NOW)
    assert vector.complete
    assert vector.as_dict() == {"return": Decimal("0.01")}


def test_materialization_rejects_out_of_range_value_without_partial_write() -> None:
    _, materializer, _ = setup_store()
    run = materializer.start(MaterializationRun("run", ("return",), NOW - timedelta(days=1), NOW + timedelta(days=1), NOW))
    value = FeatureValue("return", (("symbol", "EURUSD"),), Decimal("2"), NOW, NOW, "source-1")
    assert materializer.write(run.run_id, (value,)) == (0, 1)
    completed = materializer.complete(run.run_id, completed_at=NOW, expected_version=3)
    assert completed.state == MaterializationState.PARTIAL


def test_source_reference_conflict_is_rejected() -> None:
    _, materializer, _ = setup_store()
    run = materializer.start(MaterializationRun("run", ("return",), NOW - timedelta(days=1), NOW + timedelta(days=1), NOW))
    first = FeatureValue("return", (("symbol", "EURUSD"),), Decimal("0.01"), NOW, NOW, "source")
    second = FeatureValue("return", (("symbol", "EURUSD"),), Decimal("0.02"), NOW, NOW, "source")
    materializer.write(run.run_id, (first,))
    with pytest.raises(StorageConflictError):
        materializer.write(run.run_id, (second,))


def test_point_in_time_join_avoids_future_created_values() -> None:
    _, materializer, service = setup_store()
    run = materializer.start(MaterializationRun("run", ("return",), NOW - timedelta(days=2), NOW + timedelta(days=2), NOW))
    old = FeatureValue("return", (("symbol", "EURUSD"),), Decimal("0.01"), NOW - timedelta(hours=2), NOW - timedelta(hours=1), "old")
    future_created = FeatureValue("return", (("symbol", "EURUSD"),), Decimal("0.02"), NOW - timedelta(hours=1), NOW + timedelta(hours=1), "future")
    materializer.write(run.run_id, (old, future_created))
    vector = service.historical(("return",), {"symbol": "EURUSD"}, as_of=NOW)
    assert vector.as_dict()["return"] == Decimal("0.01")


def test_online_freshness_can_be_required() -> None:
    _, materializer, service = setup_store()
    run = materializer.start(MaterializationRun("run", ("return",), NOW - timedelta(days=2), NOW + timedelta(days=1), NOW))
    materializer.write(run.run_id, (FeatureValue("return", (("symbol", "EURUSD"),), Decimal("0.01"), NOW - timedelta(hours=3), NOW, "old"),))
    allowed = service.online(("return",), {"symbol": "EURUSD"}, at=NOW)
    blocked = service.online(("return",), {"symbol": "EURUSD"}, at=NOW, require_fresh=True)
    assert allowed.features[0].stale
    assert blocked.missing == ("return",)
