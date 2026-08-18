from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from forex_monitor.config import DatabaseConfig
from forex_monitor.errors import NotFoundError, ProviderError, StorageConflictError
from forex_monitor.ids import SequenceIdFactory
from forex_monitor.ingestion import IngestionService
from forex_monitor.models import (
    Alert,
    AlertOperator,
    AlertRule,
    AlertStatus,
    IngestionRun,
    Instrument,
    RunStatus,
    Timeframe,
)
from forex_monitor.providers import FixtureProvider
from forex_monitor.storage import Database, RepositorySet
from forex_monitor.storage.migrations import MIGRATIONS, Migration, pending_migrations, validate_migrations
from tests.support import BASE_TIME, MutableClock, TemporaryRepositories, candle, quote, quote_series


class MigrationTests(unittest.TestCase):
    def test_migrations_are_contiguous(self) -> None:
        validate_migrations()
        self.assertEqual([item.version for item in MIGRATIONS], list(range(1, len(MIGRATIONS) + 1)))

    def test_pending_migrations_filters_versions(self) -> None:
        self.assertEqual([item.version for item in pending_migrations(0)], [1, 2, 3, 4])
        self.assertEqual([item.version for item in pending_migrations(2)], [3, 4])
        self.assertEqual(tuple(pending_migrations(4)), ())

    def test_invalid_migration_sequence_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "out of order"):
            validate_migrations((Migration(2, "second", ("SELECT 1",)),))

    def test_duplicate_names_are_rejected(self) -> None:
        migrations = (
            Migration(1, "same", ("SELECT 1",)),
            Migration(2, "same", ("SELECT 2",)),
        )
        with self.assertRaisesRegex(ValueError, "duplicate"):
            validate_migrations(migrations)


class DatabaseTests(unittest.TestCase):
    def test_initialize_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(DatabaseConfig(path=Path(directory) / "db.sqlite"))
            self.assertEqual(database.schema_version(), 0)
            self.assertEqual(database.initialize(), len(MIGRATIONS))
            self.assertEqual(database.initialize(), len(MIGRATIONS))
            self.assertEqual(database.schema_version(), len(MIGRATIONS))

    def test_health_reports_current_schema(self) -> None:
        with TemporaryRepositories() as repositories:
            health = repositories.database.health()
            self.assertTrue(health["healthy"])
            self.assertEqual(health["schemaVersion"], len(MIGRATIONS))
            self.assertEqual(health["quickCheck"], "ok")

    def test_outer_transaction_rolls_back(self) -> None:
        with TemporaryRepositories() as repositories:
            with self.assertRaises(RuntimeError):
                with repositories.database.transaction(write=True) as transaction:
                    repositories.instruments.upsert(
                        Instrument.from_symbol("EURUSD"),
                        now=BASE_TIME,
                        transaction=transaction,
                    )
                    raise RuntimeError("late failure")
            self.assertIsNone(repositories.instruments.get("EURUSD"))

    def test_nested_failure_can_be_caught(self) -> None:
        with TemporaryRepositories() as repositories:
            with repositories.database.transaction(write=True) as outer:
                repositories.instruments.upsert(
                    Instrument.from_symbol("EURUSD"),
                    now=BASE_TIME,
                    transaction=outer,
                )
                try:
                    with repositories.database.transaction(write=True) as inner:
                        repositories.instruments.upsert(
                            Instrument.from_symbol("GBPUSD"),
                            now=BASE_TIME,
                            transaction=inner,
                        )
                        raise RuntimeError("rollback savepoint")
                except RuntimeError:
                    pass
            self.assertIsNotNone(repositories.instruments.get("EURUSD"))
            self.assertIsNone(repositories.instruments.get("GBPUSD"))


class InstrumentRepositoryTests(unittest.TestCase):
    def test_upsert_and_get(self) -> None:
        with TemporaryRepositories() as repositories:
            instrument = Instrument.from_symbol("EURUSD")
            repositories.instruments.upsert(instrument, now=BASE_TIME)
            self.assertEqual(repositories.instruments.get("eurusd"), instrument)

    def test_upsert_updates_mutable_fields(self) -> None:
        with TemporaryRepositories() as repositories:
            original = Instrument.from_symbol("EURUSD")
            updated = replace(original, display_name="Euro Dollar", active=False)
            repositories.instruments.upsert(original, now=BASE_TIME)
            repositories.instruments.upsert(updated, now=BASE_TIME + timedelta(seconds=1))
            self.assertEqual(repositories.instruments.require("EURUSD"), updated)

    def test_list_is_sorted_and_can_filter_active(self) -> None:
        with TemporaryRepositories() as repositories:
            repositories.instruments.upsert(Instrument.from_symbol("GBPUSD"), now=BASE_TIME)
            repositories.instruments.upsert(
                replace(Instrument.from_symbol("EURUSD"), active=False),
                now=BASE_TIME,
            )
            self.assertEqual([item.symbol for item in repositories.instruments.list()], ["EURUSD", "GBPUSD"])
            self.assertEqual([item.symbol for item in repositories.instruments.list(active_only=True)], ["GBPUSD"])

    def test_require_missing_has_typed_error(self) -> None:
        with TemporaryRepositories() as repositories:
            with self.assertRaises(NotFoundError):
                repositories.instruments.require("EURUSD")


class QuoteRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = TemporaryRepositories()
        self.repositories = self.context.__enter__()
        self.repositories.instruments.upsert(Instrument.from_symbol("EURUSD"), now=BASE_TIME)

    def tearDown(self) -> None:
        self.context.__exit__()

    def test_insert_and_duplicate(self) -> None:
        value = quote()
        self.assertTrue(self.repositories.quotes.add(value, received_at=BASE_TIME))
        self.assertFalse(self.repositories.quotes.add(value, received_at=BASE_TIME))
        self.assertEqual(self.repositories.quotes.count(), 1)

    def test_unknown_instrument_conflicts(self) -> None:
        with self.assertRaises(StorageConflictError):
            self.repositories.quotes.add(quote("GBPUSD"), received_at=BASE_TIME)

    def test_range_filters_times(self) -> None:
        values = quote_series(5)
        self.repositories.quotes.add_many(values, received_at=BASE_TIME)
        result = self.repositories.quotes.range(
            "EURUSD",
            start=BASE_TIME + timedelta(minutes=1),
            end=BASE_TIME + timedelta(minutes=4),
        )
        self.assertEqual(result, values[1:4])

    def test_range_can_descend_and_limit(self) -> None:
        values = quote_series(5)
        self.repositories.quotes.add_many(values, received_at=BASE_TIME)
        result = self.repositories.quotes.range("EURUSD", limit=2, ascending=False)
        self.assertEqual(result, tuple(reversed(values[-2:])))

    def test_latest_can_filter_provider(self) -> None:
        first = quote(provider="one")
        second = quote(
            provider="two",
            source_id="second",
            observed_at=BASE_TIME + timedelta(seconds=1),
        )
        self.repositories.quotes.add_many((first, second), received_at=BASE_TIME)
        self.assertEqual(self.repositories.quotes.latest("EURUSD"), second)
        self.assertEqual(self.repositories.quotes.latest("EURUSD", provider="one"), first)

    def test_delete_before_uses_batch_size(self) -> None:
        values = quote_series(5)
        self.repositories.quotes.add_many(values, received_at=BASE_TIME)
        deleted = self.repositories.quotes.delete_before(
            BASE_TIME + timedelta(minutes=5),
            batch_size=2,
        )
        self.assertEqual(deleted, 2)
        self.assertEqual(self.repositories.quotes.count(), 3)


class CandleRepositoryTests(unittest.TestCase):
    def test_upsert_replaces_same_bucket(self) -> None:
        with TemporaryRepositories() as repositories:
            repositories.instruments.upsert(Instrument.from_symbol("EURUSD"), now=BASE_TIME)
            original = candle()
            updated = replace(original, close=original.close + Decimal("0.001"), high=original.high + Decimal("0.001"))
            repositories.candles.upsert(original, now=BASE_TIME)
            repositories.candles.upsert(updated, now=BASE_TIME + timedelta(seconds=1))
            self.assertEqual(repositories.candles.range("EURUSD", Timeframe.HOUR_1), (updated,))

    def test_range_filters_open_time(self) -> None:
        with TemporaryRepositories() as repositories:
            repositories.instruments.upsert(Instrument.from_symbol("EURUSD"), now=BASE_TIME)
            values = tuple(candle(index) for index in range(4))
            for value in values:
                repositories.candles.upsert(value, now=BASE_TIME)
            result = repositories.candles.range(
                "EURUSD",
                Timeframe.HOUR_1,
                start=BASE_TIME + timedelta(hours=1),
                end=BASE_TIME + timedelta(hours=3),
            )
            self.assertEqual(result, values[1:3])


class RunRepositoryTests(unittest.TestCase):
    def test_create_update_and_recent(self) -> None:
        with TemporaryRepositories() as repositories:
            first = IngestionRun("run_1", "fixture", BASE_TIME)
            second = IngestionRun("run_2", "fixture", BASE_TIME + timedelta(seconds=1))
            repositories.runs.create(first)
            repositories.runs.create(second)
            completed = second.finish(status=RunStatus.SUCCEEDED, finished_at=second.started_at)
            repositories.runs.update(completed)
            self.assertEqual(repositories.runs.get("run_2"), completed)
            self.assertEqual(repositories.runs.recent(1), (completed,))

    def test_duplicate_run_conflicts(self) -> None:
        with TemporaryRepositories() as repositories:
            run = IngestionRun("run_1", "fixture", BASE_TIME)
            repositories.runs.create(run)
            with self.assertRaises(StorageConflictError):
                repositories.runs.create(run)

    def test_update_missing_run(self) -> None:
        with TemporaryRepositories() as repositories:
            run = IngestionRun("run_1", "fixture", BASE_TIME).finish(
                status=RunStatus.FAILED,
                finished_at=BASE_TIME,
            )
            with self.assertRaises(NotFoundError):
                repositories.runs.update(run)


class AlertRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = TemporaryRepositories()
        self.repositories = self.context.__enter__()
        self.repositories.instruments.upsert(Instrument.from_symbol("EURUSD"), now=BASE_TIME)
        self.rule = AlertRule(
            "rule_1", "High", "EURUSD", "mid", AlertOperator.GREATER_THAN,
            Decimal("1.2"), created_at=BASE_TIME, updated_at=BASE_TIME,
        )
        self.repositories.alerts.save_rule(self.rule)

    def tearDown(self) -> None:
        self.context.__exit__()

    def test_rule_round_trip(self) -> None:
        self.assertEqual(self.repositories.alerts.get_rule("rule_1"), self.rule)
        self.assertEqual(self.repositories.alerts.rules_for("EURUSD"), (self.rule,))

    def test_disabled_rule_filter(self) -> None:
        disabled = replace(self.rule, enabled=False)
        self.repositories.alerts.save_rule(disabled)
        self.assertEqual(self.repositories.alerts.rules_for("EURUSD"), ())
        self.assertEqual(self.repositories.alerts.rules_for("EURUSD", enabled_only=False), (disabled,))

    def test_alert_lifecycle(self) -> None:
        alert = Alert(
            "alert_1", self.rule.id, "EURUSD", "mid", Decimal("1.3"),
            self.rule.threshold, self.rule.operator, BASE_TIME, "message",
        )
        self.repositories.alerts.add_alert(alert)
        self.assertEqual(self.repositories.alerts.pending(), (alert,))
        delivered = replace(alert, status=AlertStatus.DELIVERED, delivered_at=BASE_TIME)
        self.repositories.alerts.update_alert(delivered)
        self.assertEqual(self.repositories.alerts.pending(), ())


class IngestionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = TemporaryRepositories()
        self.repositories = self.context.__enter__()
        self.clock = MutableClock()
        self.identifiers = SequenceIdFactory("run")

    def tearDown(self) -> None:
        self.context.__exit__()

    def service(self, batches: list[object], **kwargs: object) -> IngestionService:
        return IngestionService(
            FixtureProvider(batches),
            self.repositories,
            clock=self.clock,
            identifiers=self.identifiers,
            **kwargs,
        )

    def test_success_registers_instrument_and_quote(self) -> None:
        outcome = self.service([(quote(),)]).ingest(("EURUSD",))
        self.assertEqual(outcome.run.status, RunStatus.SUCCEEDED)
        self.assertEqual(outcome.run.imported_quotes, 1)
        self.assertIsNotNone(self.repositories.instruments.get("EURUSD"))
        self.assertEqual(self.repositories.quotes.count(), 1)

    def test_duplicate_retry_is_audited_without_second_insert(self) -> None:
        value = quote()
        service = self.service([(value,), (value,)])
        first = service.ingest(("EURUSD",))
        second = service.ingest(("EURUSD",))
        self.assertEqual(first.run.imported_quotes, 1)
        self.assertEqual(second.run.duplicate_quotes, 1)
        self.assertEqual(self.repositories.quotes.count(), 1)

    def test_missing_symbol_is_partial(self) -> None:
        outcome = self.service([(quote(),)]).ingest(("EURUSD", "GBPUSD"))
        self.assertEqual(outcome.run.status, RunStatus.PARTIAL)
        self.assertEqual(outcome.missing_symbols, ("GBPUSD",))
        self.assertEqual(outcome.run.rejected_quotes, 1)

    def test_require_all_rejects_missing_before_write(self) -> None:
        service = self.service([(quote(),)], require_all_symbols=True)
        with self.assertRaisesRegex(Exception, "missing"):
            service.ingest(("EURUSD", "GBPUSD"))
        self.assertEqual(self.repositories.quotes.count(), 0)
        self.assertEqual(self.repositories.runs.recent(1)[0].status, RunStatus.FAILED)

    def test_provider_failure_is_audited(self) -> None:
        service = self.service([ProviderError("offline")])
        with self.assertRaisesRegex(ProviderError, "offline"):
            service.ingest(("EURUSD",))
        run = self.repositories.runs.recent(1)[0]
        self.assertEqual(run.status, RunStatus.FAILED)
        self.assertEqual(run.error_code, "provider_error")

    def test_unrequested_quote_is_rejected(self) -> None:
        class UnrequestedProvider:
            name = "fixture"

            def fetch_quotes(self, symbols: object) -> tuple[object, ...]:
                return (quote("GBPUSD"),)

        service = IngestionService(
            UnrequestedProvider(),  # type: ignore[arg-type]
            self.repositories,
            clock=self.clock,
            identifiers=self.identifiers,
        )
        with self.assertRaisesRegex(Exception, "unrequested"):
            service.ingest(("EURUSD",))
        self.assertEqual(self.repositories.quotes.count(), 0)

    def test_seed_instruments_is_atomic(self) -> None:
        result = self.service([(quote(),)]).seed_instruments(("EURUSD", "USDJPY"))
        self.assertEqual([item.symbol for item in result], ["EURUSD", "USDJPY"])
        self.assertEqual(len(self.repositories.instruments.list()), 2)


if __name__ == "__main__":
    unittest.main()
