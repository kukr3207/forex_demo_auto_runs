from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from forex_monitor.config import AppConfig, DatabaseConfig, ProviderConfig, RuntimeConfig
from forex_monitor.errors import ConfigurationError
from forex_monitor.models import Timeframe
from forex_monitor.timeutils import (
    TimeWindow,
    ceil_time,
    floor_time,
    format_duration,
    is_forex_market_open,
    iter_buckets,
    merge_windows,
    missing_windows,
    next_market_open,
    next_utc_midnight,
    parse_duration,
    utc_day_bounds,
    utc_week_bounds,
)
from tests.support import BASE_TIME


class ConfigurationTests(unittest.TestCase):
    def test_environment_defaults_are_valid(self) -> None:
        config = AppConfig.from_environment({})
        self.assertEqual(config.database.path, Path("forex_monitor.db"))
        self.assertEqual(config.provider.name, "fxssi")
        self.assertIn("EURUSD", config.runtime.instruments)

    def test_environment_overrides_are_normalized(self) -> None:
        config = AppConfig.from_environment(
            {
                "FOREX_DATABASE_PATH": "/tmp/example.db",
                "FOREX_DATABASE_JOURNAL_MODE": "delete",
                "FOREX_PROVIDER": " Demo ",
                "FOREX_PROVIDER_URL": "http://localhost:9000/quotes/",
                "FOREX_PROVIDER_TIMEOUT": "2.5",
                "FOREX_PROVIDER_MAX_ATTEMPTS": "4",
                "FOREX_INSTRUMENTS": "eurusd, GBPUSD,eurusd",
                "FOREX_INTERVAL_SECONDS": "60",
                "FOREX_RETENTION_DAYS": "30",
                "FOREX_BATCH_SIZE": "25",
                "FOREX_FAIL_FAST": "yes",
                "FOREX_LOG_LEVEL": "debug",
            }
        )
        self.assertEqual(config.database.journal_mode, "DELETE")
        self.assertEqual(config.provider.name, "demo")
        self.assertEqual(config.provider.base_url, "http://localhost:9000/quotes")
        self.assertEqual(config.provider.timeout_seconds, 2.5)
        self.assertEqual(config.runtime.instruments, ("EURUSD", "GBPUSD"))
        self.assertTrue(config.runtime.fail_fast)
        self.assertEqual(config.runtime.log_level, "DEBUG")

    def test_mapping_requires_nested_objects(self) -> None:
        for key in ("database", "provider", "runtime"):
            with self.subTest(key=key), self.assertRaises(ConfigurationError):
                AppConfig.from_mapping({key: "not an object"})

    def test_database_validation(self) -> None:
        with self.assertRaises(ConfigurationError):
            DatabaseConfig(busy_timeout_seconds=0)
        with self.assertRaises(ConfigurationError):
            DatabaseConfig(journal_mode="unknown")

    def test_provider_validation(self) -> None:
        bad = [
            {"base_url": "ftp://example.com"},
            {"timeout_seconds": 0},
            {"max_attempts": 0},
            {"max_attempts": 11},
            {"initial_backoff_seconds": -1},
        ]
        for kwargs in bad:
            with self.subTest(kwargs=kwargs), self.assertRaises(ConfigurationError):
                ProviderConfig(**kwargs)

    def test_runtime_validation(self) -> None:
        bad = [
            {"instruments": ()},
            {"interval_seconds": 0},
            {"retention_days": 0},
            {"batch_size": 0},
            {"batch_size": 100_001},
            {"log_level": "trace"},
        ]
        for kwargs in bad:
            with self.subTest(kwargs=kwargs), self.assertRaises(ConfigurationError):
                RuntimeConfig(**kwargs)


class BoundaryTests(unittest.TestCase):
    def test_floor_and_ceil(self) -> None:
        value = datetime(2026, 1, 1, 10, 37, 25, tzinfo=timezone.utc)
        self.assertEqual(floor_time(value, Timeframe.HOUR_1).hour, 10)
        self.assertEqual(ceil_time(value, Timeframe.HOUR_1).hour, 11)
        boundary = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
        self.assertEqual(ceil_time(boundary, Timeframe.HOUR_1), boundary)

    def test_day_and_week_bounds(self) -> None:
        value = datetime(2026, 1, 7, 15, tzinfo=timezone.utc)
        day_start, day_end = utc_day_bounds(value)
        week_start, week_end = utc_week_bounds(value)
        self.assertEqual(day_end - day_start, timedelta(days=1))
        self.assertEqual(week_start.weekday(), 0)
        self.assertEqual(week_end - week_start, timedelta(days=7))
        self.assertEqual(next_utc_midnight(value), day_end)

    def test_bucket_iteration(self) -> None:
        start = datetime(2026, 1, 1, 10, 15, tzinfo=timezone.utc)
        end = datetime(2026, 1, 1, 12, 1, tzinfo=timezone.utc)
        buckets = tuple(iter_buckets(start, end, Timeframe.HOUR_1))
        self.assertEqual(len(buckets), 3)
        self.assertEqual(buckets[0][0].hour, 10)
        self.assertEqual(buckets[-1][0].hour, 12)


class DurationTests(unittest.TestCase):
    def test_duration_round_trip_for_exact_units(self) -> None:
        expected = {"30s": "30s", "5m": "5m", "2h": "2h", "7d": "1w", "3w": "3w"}
        for text, formatted in expected.items():
            with self.subTest(text=text):
                self.assertEqual(format_duration(parse_duration(text)), formatted)

    def test_invalid_durations_are_rejected(self) -> None:
        for value in ("", "5", "0h", "1x", "1.5h", "999999w"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_duration(value)


class MarketSessionTests(unittest.TestCase):
    def test_weekday_is_open(self) -> None:
        self.assertTrue(is_forex_market_open(datetime(2026, 1, 6, 12, tzinfo=timezone.utc)))

    def test_weekend_boundaries(self) -> None:
        friday_before = datetime(2026, 1, 9, 21, 59, tzinfo=timezone.utc)
        friday_after = datetime(2026, 1, 9, 22, 0, tzinfo=timezone.utc)
        sunday_before = datetime(2026, 1, 11, 21, 59, tzinfo=timezone.utc)
        sunday_after = datetime(2026, 1, 11, 22, 0, tzinfo=timezone.utc)
        self.assertTrue(is_forex_market_open(friday_before))
        self.assertFalse(is_forex_market_open(friday_after))
        self.assertFalse(is_forex_market_open(sunday_before))
        self.assertTrue(is_forex_market_open(sunday_after))

    def test_next_open_from_saturday(self) -> None:
        saturday = datetime(2026, 1, 10, 10, tzinfo=timezone.utc)
        self.assertEqual(
            next_market_open(saturday),
            datetime(2026, 1, 11, 22, tzinfo=timezone.utc),
        )


class WindowTests(unittest.TestCase):
    def test_contains_and_overlap_are_half_open(self) -> None:
        window = TimeWindow(BASE_TIME, BASE_TIME + timedelta(hours=2))
        self.assertTrue(window.contains(BASE_TIME))
        self.assertFalse(window.contains(window.end))
        touching = TimeWindow(window.end, window.end + timedelta(hours=1))
        self.assertFalse(window.overlaps(touching))
        self.assertIsNone(window.intersection(touching))

    def test_merge_overlapping_and_touching_windows(self) -> None:
        first = TimeWindow(BASE_TIME, BASE_TIME + timedelta(hours=2))
        second = TimeWindow(BASE_TIME + timedelta(hours=1), BASE_TIME + timedelta(hours=3))
        third = TimeWindow(BASE_TIME + timedelta(hours=3), BASE_TIME + timedelta(hours=4))
        self.assertEqual(
            merge_windows((third, first, second)), (TimeWindow(first.start, third.end),)
        )

    def test_missing_windows(self) -> None:
        requested = TimeWindow(BASE_TIME, BASE_TIME + timedelta(hours=6))
        available = (
            TimeWindow(BASE_TIME + timedelta(hours=1), BASE_TIME + timedelta(hours=2)),
            TimeWindow(BASE_TIME + timedelta(hours=4), BASE_TIME + timedelta(hours=5)),
        )
        missing = missing_windows(requested, available)
        self.assertEqual(len(missing), 3)
        self.assertEqual(missing[0], TimeWindow(BASE_TIME, BASE_TIME + timedelta(hours=1)))
        self.assertEqual(missing[-1].end, requested.end)

    def test_split_uses_timeframe_buckets(self) -> None:
        window = TimeWindow(BASE_TIME, BASE_TIME + timedelta(hours=2))
        pieces = window.split(Timeframe.HOUR_1)
        self.assertEqual(len(pieces), 2)
        self.assertEqual(pieces[0].end, pieces[1].start)


if __name__ == "__main__":
    unittest.main()
