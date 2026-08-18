from __future__ import annotations

import io
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from forex_monitor.alerts import (
    AlertService,
    CompositeNotifier,
    ConsoleNotifier,
    MemoryNotifier,
    RuleEvaluator,
)
from forex_monitor.alerts.evaluator import render_alert_message
from forex_monitor.alerts.notifications import JsonlFileNotifier
from forex_monitor.errors import AlertEvaluationError, NotFoundError
from forex_monitor.ids import SequenceIdFactory
from forex_monitor.models import Alert, AlertOperator, AlertRule, Instrument
from forex_monitor.scheduler import (
    JobRepository,
    JobStatus,
    LeaseRepository,
    ScheduledJob,
    Scheduler,
)
from tests.support import BASE_TIME, MutableClock, TemporaryRepositories, quote


def rule(operator: AlertOperator, threshold: object = "1.2", **kwargs: object) -> AlertRule:
    return AlertRule(
        "rule_1",
        "Price rule",
        "EURUSD",
        "mid",
        operator,
        Decimal(str(threshold)),
        created_at=BASE_TIME,
        updated_at=BASE_TIME,
        **kwargs,
    )


def alert() -> Alert:
    return Alert(
        "alert_1",
        "rule_1",
        "EURUSD",
        "mid",
        Decimal("1.3"),
        Decimal("1.2"),
        AlertOperator.GREATER_THAN,
        BASE_TIME,
        "EURUSD is high",
    )


class RuleEvaluatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.evaluator = RuleEvaluator()

    def test_simple_operators(self) -> None:
        cases = (
            (AlertOperator.GREATER_THAN, "1.3", True),
            (AlertOperator.GREATER_THAN, "1.2", False),
            (AlertOperator.GREATER_OR_EQUAL, "1.2", True),
            (AlertOperator.LESS_THAN, "1.1", True),
            (AlertOperator.LESS_THAN, "1.2", False),
            (AlertOperator.LESS_OR_EQUAL, "1.2", True),
        )
        for operator, current, matched in cases:
            with self.subTest(operator=operator, current=current):
                result = self.evaluator.evaluate(
                    rule(operator),
                    Decimal(current),
                    evaluated_at=BASE_TIME,
                )
                self.assertEqual(result.matched, matched)

    def test_crossing_operators(self) -> None:
        above = self.evaluator.evaluate(
            rule(AlertOperator.CROSSES_ABOVE),
            Decimal("1.3"),
            previous_value=Decimal("1.1"),
            evaluated_at=BASE_TIME,
        )
        below = self.evaluator.evaluate(
            rule(AlertOperator.CROSSES_BELOW),
            Decimal("1.1"),
            previous_value=Decimal("1.3"),
            evaluated_at=BASE_TIME,
        )
        self.assertTrue(above.should_trigger)
        self.assertTrue(below.should_trigger)

    def test_crossing_without_previous_does_not_match(self) -> None:
        result = self.evaluator.evaluate(
            rule(AlertOperator.CROSSES_ABOVE),
            Decimal("1.3"),
            evaluated_at=BASE_TIME,
        )
        self.assertFalse(result.matched)
        self.assertIn("previous", result.reason)

    def test_disabled_rule_is_suppressed(self) -> None:
        result = self.evaluator.evaluate(
            rule(AlertOperator.GREATER_THAN, enabled=False),
            Decimal("2"),
            evaluated_at=BASE_TIME,
        )
        self.assertTrue(result.suppressed)
        self.assertFalse(result.should_trigger)

    def test_cooldown_suppresses_then_expires(self) -> None:
        configured = replace(
            rule(AlertOperator.GREATER_THAN, cooldown_seconds=60),
            last_triggered_at=BASE_TIME,
        )
        suppressed = self.evaluator.evaluate(
            configured,
            Decimal("2"),
            evaluated_at=BASE_TIME + timedelta(seconds=59),
        )
        eligible = self.evaluator.evaluate(
            configured,
            Decimal("2"),
            evaluated_at=BASE_TIME + timedelta(seconds=60),
        )
        self.assertTrue(suppressed.suppressed)
        self.assertTrue(eligible.should_trigger)

    def test_message_requires_trigger(self) -> None:
        configured = rule(AlertOperator.GREATER_THAN)
        result = self.evaluator.evaluate(configured, Decimal("1"), evaluated_at=BASE_TIME)
        with self.assertRaises(AlertEvaluationError):
            render_alert_message(configured, result)


class NotifierTests(unittest.TestCase):
    def test_memory_notifier_collects_alert(self) -> None:
        notifier = MemoryNotifier(clock=lambda: BASE_TIME)
        result = notifier.notify(alert())
        self.assertTrue(result.delivered)
        self.assertEqual(notifier.alerts, [alert()])

    def test_memory_notifier_can_fail(self) -> None:
        notifier = MemoryNotifier(clock=lambda: BASE_TIME, fail_with="offline")
        result = notifier.notify(alert())
        self.assertFalse(result.delivered)
        self.assertEqual(result.error, "offline")

    def test_console_notifier_writes_json(self) -> None:
        stream = io.StringIO()
        result = ConsoleNotifier(stream, clock=lambda: BASE_TIME).notify(alert())
        self.assertTrue(result.delivered)
        self.assertIn('"alert_1"', stream.getvalue())

    def test_jsonl_file_notifier_appends(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "alerts.jsonl"
            notifier = JsonlFileNotifier(path, clock=lambda: BASE_TIME)
            notifier.notify(alert())
            notifier.notify(alert())
            self.assertEqual(len(path.read_text().splitlines()), 2)

    def test_composite_require_all(self) -> None:
        composite = CompositeNotifier(
            (
                MemoryNotifier("one", clock=lambda: BASE_TIME),
                MemoryNotifier("two", clock=lambda: BASE_TIME, fail_with="no"),
            )
        )
        result = composite.notify(alert())
        self.assertFalse(result.delivered)
        self.assertIn("two", result.error or "")

    def test_composite_any_can_succeed(self) -> None:
        composite = CompositeNotifier(
            (
                MemoryNotifier("one", clock=lambda: BASE_TIME),
                MemoryNotifier("two", clock=lambda: BASE_TIME, fail_with="no"),
            ),
            require_all=False,
        )
        self.assertTrue(composite.notify(alert()).delivered)


class AlertServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = TemporaryRepositories()
        self.repositories = self.context.__enter__()
        self.repositories.instruments.upsert(Instrument.from_symbol("EURUSD"), now=BASE_TIME)
        self.rule = rule(AlertOperator.GREATER_THAN, threshold="1.1")
        self.repositories.alerts.save_rule(self.rule)
        self.notifier = MemoryNotifier(clock=lambda: BASE_TIME)
        self.service = AlertService(
            self.repositories.database,
            self.repositories.alerts,
            self.notifier,
            clock=lambda: BASE_TIME,
            identifiers=SequenceIdFactory("alert"),
        )

    def tearDown(self) -> None:
        self.context.__exit__()

    def test_quote_trigger_creates_pending_alert_and_updates_rule(self) -> None:
        triggered = self.service.evaluate_quote(quote(bid="1.2", ask="1.3"))
        self.assertEqual(len(triggered), 1)
        self.assertEqual(self.repositories.alerts.pending(), triggered)
        updated = self.repositories.alerts.get_rule(self.rule.id)
        self.assertEqual(updated.last_triggered_at, BASE_TIME)  # type: ignore[union-attr]

    def test_unmatched_quote_has_no_write(self) -> None:
        self.assertEqual(self.service.evaluate_quote(quote(bid="1.0", ask="1.1")), ())
        self.assertEqual(self.repositories.alerts.pending(), ())

    def test_deliver_pending_marks_delivered(self) -> None:
        self.service.evaluate_quote(quote(bid="1.2", ask="1.3"))
        summary = self.service.deliver_pending()
        self.assertEqual(summary.attempted, 1)
        self.assertEqual(summary.delivered, 1)
        self.assertEqual(self.repositories.alerts.pending(), ())


class LeaseTests(unittest.TestCase):
    def test_only_one_owner_holds_active_lease(self) -> None:
        with TemporaryRepositories() as repositories:
            leases = LeaseRepository(repositories.database)
            self.assertTrue(leases.acquire("job:x", "one", now=BASE_TIME, ttl_seconds=60))
            self.assertFalse(leases.acquire("job:x", "two", now=BASE_TIME, ttl_seconds=60))
            self.assertTrue(
                leases.acquire(
                    "job:x", "two", now=BASE_TIME + timedelta(seconds=60), ttl_seconds=60
                )
            )

    def test_owner_can_extend_and_release(self) -> None:
        with TemporaryRepositories() as repositories:
            leases = LeaseRepository(repositories.database)
            leases.acquire("job:x", "one", now=BASE_TIME, ttl_seconds=60)
            self.assertFalse(leases.extend("job:x", "two", now=BASE_TIME, ttl_seconds=60))
            self.assertTrue(leases.extend("job:x", "one", now=BASE_TIME, ttl_seconds=120))
            self.assertTrue(leases.release("job:x", "one"))
            self.assertFalse(leases.release("job:x", "one"))


class SchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = TemporaryRepositories()
        self.repositories = self.context.__enter__()
        self.jobs = JobRepository(self.repositories.database)
        self.leases = LeaseRepository(self.repositories.database)
        self.clock = MutableClock()
        self.calls: list[str] = []

    def tearDown(self) -> None:
        self.context.__exit__()

    def scheduler(self, handler: object) -> Scheduler:
        return Scheduler(
            self.repositories.database,
            self.jobs,
            self.leases,
            {"demo": handler},  # type: ignore[dict-item]
            clock=self.clock,
            identifiers=SequenceIdFactory("owner"),
        )

    def test_due_order_and_limit(self) -> None:
        self.jobs.save(ScheduledJob("b", "demo", 60, True, BASE_TIME))
        self.jobs.save(ScheduledJob("a", "demo", 60, True, BASE_TIME))
        self.assertEqual([job.id for job in self.jobs.due(BASE_TIME, limit=1)], ["a"])

    def test_success_advances_schedule(self) -> None:
        self.jobs.save(ScheduledJob("job_1", "demo", 60, True, BASE_TIME))
        scheduler = self.scheduler(lambda job: self.calls.append(job.id) or "done")
        execution = scheduler.run_job("job_1")
        self.assertEqual(execution.status, JobStatus.SUCCEEDED)  # type: ignore[union-attr]
        self.assertEqual(self.calls, ["job_1"])
        self.assertEqual(self.jobs.get("job_1").next_run_at, BASE_TIME + timedelta(seconds=60))  # type: ignore[union-attr]

    def test_failure_is_recorded_and_does_not_raise(self) -> None:
        self.jobs.save(ScheduledJob("job_1", "demo", 60, True, BASE_TIME))

        def fail(job: ScheduledJob) -> str:
            raise RuntimeError("boom")

        execution = self.scheduler(fail).run_job("job_1")
        self.assertEqual(execution.status, JobStatus.FAILED)  # type: ignore[union-attr]
        self.assertEqual(self.jobs.get("job_1").consecutive_failures, 1)  # type: ignore[union-attr]

    def test_disabled_job_is_skipped(self) -> None:
        self.jobs.save(ScheduledJob("job_1", "demo", 60, False, BASE_TIME))
        self.assertIsNone(self.scheduler(lambda job: "done").run_job("job_1"))

    def test_missing_job_and_handler_have_typed_errors(self) -> None:
        scheduler = self.scheduler(lambda job: "done")
        with self.assertRaises(NotFoundError):
            scheduler.run_job("missing")
        self.jobs.save(ScheduledJob("job_1", "unknown", 60, True, BASE_TIME))
        with self.assertRaises(NotFoundError):
            scheduler.run_job("job_1")


if __name__ == "__main__":
    unittest.main()
