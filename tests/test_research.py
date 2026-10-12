from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from forex_monitor.errors import StorageConflictError, ValidationError
from forex_monitor.research.datasets import Dataset, DatasetCatalog, Observation
from forex_monitor.research.experiments import Experiment, ExperimentRegistry, Trial, TrialState
from forex_monitor.research.features import FeatureDefinition, FeaturePipeline
from forex_monitor.research.validation import WalkForwardValidator


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def observations(count=30):
    return tuple(
        Observation(NOW + timedelta(days=index), "EURUSD", {"close": Decimal("1") + Decimal(index) / 100}, "fixture")
        for index in range(count)
    )


class DatasetTests(unittest.TestCase):
    def test_dataset_digest_filter_and_coverage(self):
        dataset = Dataset("prices", 1, observations(3), NOW, tags=("Daily", "daily"))
        self.assertEqual(len(dataset.digest), 64)
        self.assertEqual(dataset.tags, ("daily",))
        self.assertEqual(dataset.symbols, ("EURUSD",))
        self.assertEqual(len(dataset.filter(start=NOW + timedelta(days=1))), 2)
        self.assertEqual(dataset.field_coverage(), {"close": Decimal("1")})

    def test_catalog_enforces_version_lineage(self):
        catalog = DatasetCatalog()
        first = Dataset("prices", 1, observations(2), NOW)
        catalog.publish(first)
        second = Dataset("prices", 2, observations(3), NOW + timedelta(days=1), parent_digest=first.digest)
        catalog.publish(second)
        self.assertEqual(catalog.get("prices"), second)
        self.assertEqual(catalog.lineage("prices"), (first, second))
        with self.assertRaises(ValidationError):
            catalog.publish(Dataset("prices", 4, observations(4), NOW, parent_digest=second.digest))


class FeatureTests(unittest.TestCase):
    def test_standard_features_are_lagged_and_ordered(self):
        rows = FeaturePipeline.standard_price_features().transform(observations(30))
        self.assertEqual(len(rows), 30)
        self.assertEqual(rows[0].values, {"price": Decimal("1")})
        self.assertIn("return_1", rows[1].values)
        self.assertIn("ma_5", rows[4].values)
        self.assertIn("volatility_20", rows[20].values)

    def test_dependencies_must_be_registered(self):
        pipeline = FeaturePipeline()
        with self.assertRaises(ValidationError):
            pipeline.add(FeatureDefinition("derived", lambda values, index: Decimal("1"), dependencies=("missing",)))


class ExperimentTests(unittest.TestCase):
    def test_trial_lifecycle_and_best_selection(self):
        registry = ExperimentRegistry()
        registry.create(Experiment("exp", "search", "digest", "revision", "sharpe"))
        registry.add_trial("exp", Trial("a", {"window": 10}))
        registry.add_trial("exp", Trial("b", {"window": 20}))
        for identifier, score in (("a", "1.2"), ("b", "1.5")):
            registry.start("exp", identifier, NOW)
            registry.complete("exp", identifier, NOW + timedelta(minutes=1), {"sharpe": score})
        self.assertEqual(registry.get("exp").best.trial_id, "b")

    def test_duplicate_parameters_are_rejected(self):
        registry = ExperimentRegistry()
        registry.create(Experiment("exp", "search", "digest", "revision", "score"))
        registry.add_trial("exp", Trial("a", {"x": 1}))
        with self.assertRaises(StorageConflictError):
            registry.add_trial("exp", Trial("b", {"x": 1}))


class ValidationTests(unittest.TestCase):
    def test_purged_walk_forward_splits_and_evaluates(self):
        values = observations(30)
        validator = WalkForwardValidator(folds=3, purge=timedelta(days=1))
        folds = validator.split([item.observed_at for item in values])
        self.assertEqual(len(folds), 3)
        self.assertLess(folds[0].train_end, folds[0].test_start - timedelta(days=1))
        results = validator.evaluate(
            values,
            lambda item: item.observed_at,
            lambda train, test: {"ratio": Decimal(len(test)) / Decimal(len(train))},
        )
        aggregate = validator.aggregate(results)
        self.assertIn("ratio_mean", aggregate)
        self.assertLessEqual(aggregate["ratio_min"], aggregate["ratio_max"])


if __name__ == "__main__":
    unittest.main()
