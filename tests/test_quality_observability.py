"""Data quality and operational observability behavior."""

import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from forex_monitor.observability import (
    AuditLog,
    EventLevel,
    HealthCheck,
    HealthStatus,
    LatencyTracker,
    MetricRegistry,
    ObservabilityService,
    OperationalEvent,
    overall_status,
    redact_mapping,
    run_checks,
    runtime_diagnostics,
)
from forex_monitor.quality import (
    DataQualityService,
    IssueSeverity,
    QualityIssue,
    detect_outliers,
    issue_counts,
    median_absolute_deviation,
    missing_intervals,
    severity_counts,
    validate_candles,
    validate_quotes,
)
from tests.support import BASE_TIME, candle, quote


class DataQualityTests(unittest.TestCase):
    def test_quote_rules_detect_wide_and_out_of_order(self) -> None:
        first = quote(bid="1", ask="1.10")
        second = quote(observed_at=BASE_TIME.replace(hour=11), source_id="source_2")
        issues = validate_quotes((first, second))
        self.assertEqual({issue.code for issue in issues}, {"wide_spread", "out_of_order_quote"})

    def test_candle_rules_and_gap_detection(self) -> None:
        first = replace(candle(0), complete=False, volume=0)
        third = candle(2)
        issues = validate_candles((first, third))
        self.assertEqual({issue.code for issue in issues}, {"incomplete_candle", "zero_volume"})
        gaps = missing_intervals((third, first))
        self.assertEqual(gaps[0].missing_buckets, 1)
        self.assertEqual(missing_intervals((first,)), ())

    def test_quality_report_and_summaries(self) -> None:
        report = DataQualityService().audit((quote(),), (replace(candle(), volume=0),))
        self.assertTrue(report.valid)
        self.assertEqual(issue_counts(report.issues), {"zero_volume": 1})
        self.assertEqual(severity_counts(report.issues), {"warning": 1})
        error = QualityIssue("bad", "bad row", IssueSeverity.ERROR, "row")
        self.assertFalse(DataQualityService().audit().issues)
        self.assertEqual(error.severity, IssueSeverity.ERROR)
        with self.assertRaises(ValueError):
            QualityIssue("", "bad", IssueSeverity.ERROR, "row")

    def test_outlier_detection(self) -> None:
        values = (1, 2, 3, 4, 100)
        self.assertEqual(median_absolute_deviation(values), 1)
        outliers = detect_outliers(values)
        self.assertEqual((outliers[0].index, outliers[0].value), (4, 100))
        self.assertEqual(detect_outliers((2, 2, 2)), ())
        self.assertEqual(detect_outliers(()), ())
        with self.assertRaises(ValueError):
            median_absolute_deviation(())


class ObservabilityTests(unittest.TestCase):
    def test_health_checks_and_status(self) -> None:
        checks = run_checks(
            (
                lambda: HealthCheck("database", HealthStatus.HEALTHY),
                lambda: HealthCheck("provider", HealthStatus.DEGRADED, "slow"),
            )
        )
        self.assertEqual(overall_status(checks), HealthStatus.DEGRADED)

        def broken() -> HealthCheck:
            raise RuntimeError("offline")

        failed = run_checks((broken,))
        self.assertEqual(overall_status(failed), HealthStatus.UNHEALTHY)
        self.assertEqual(overall_status(()), HealthStatus.HEALTHY)

    def test_metric_registry(self) -> None:
        metrics = MetricRegistry()
        self.assertEqual(metrics.increment("Quotes Imported"), 1)
        self.assertEqual(metrics.increment("quotes imported", 2), 3)
        self.assertEqual(metrics.gauge("spread", "0.01"), Decimal("0.01"))
        self.assertEqual(metrics.snapshot()["counters"], {"quotes_imported": 3})
        with self.assertRaises(ValueError):
            metrics.increment("x", -1)
        with self.assertRaises(ValueError):
            metrics.gauge("", 1)

    def test_events_and_bounded_audit(self) -> None:
        audit = AuditLog(capacity=2)
        for index in range(3):
            audit.append(
                OperationalEvent(
                    f"event_{index}",
                    EventLevel.INFO if index < 2 else EventLevel.ERROR,
                    BASE_TIME,
                    {"index": index},
                )
            )
        self.assertEqual(len(audit.list()), 2)
        self.assertEqual(len(audit.list(EventLevel.ERROR)), 1)
        self.assertEqual(audit.list()[-1].as_dict()["name"], "event_2")
        with self.assertRaises(ValueError):
            AuditLog(0)
        with self.assertRaises(ValueError):
            audit.list(limit=0)

    def test_diagnostics_and_redaction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "monitor.db"
            before = runtime_diagnostics(path)
            path.write_bytes(b"database")
            after = runtime_diagnostics(path)
            self.assertFalse(before["databaseExists"])
            self.assertEqual(after["databaseSize"], 8)
        self.assertEqual(
            redact_mapping({"api_key": "secret", "name": "demo"})["api_key"], "[redacted]"
        )

    def test_latency_tracker_and_service(self) -> None:
        times = iter((1.0, 1.5))
        tracker = LatencyTracker(clock=lambda: next(times))
        self.assertEqual(tracker.measure(lambda: "ok"), "ok")
        self.assertEqual(tracker.summary()["median"], 0.5)
        tracker.reset()
        self.assertEqual(tracker.summary()["count"], 0)

        metrics = MetricRegistry()
        metrics.increment("runs")
        audit = AuditLog()
        audit.append(OperationalEvent("started", EventLevel.INFO, BASE_TIME))
        snapshot = ObservabilityService(metrics, audit).snapshot(Path("missing.db"))
        self.assertEqual(snapshot["status"], "healthy")
        self.assertEqual(len(snapshot["recentEvents"]), 1)


if __name__ == "__main__":
    unittest.main()
