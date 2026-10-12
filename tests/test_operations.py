from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from forex_monitor.errors import StorageConflictError, ValidationError
from forex_monitor.operations.incidents import (
    Incident, IncidentManager, IncidentSeverity, IncidentState, TimelineEntry,
)
from forex_monitor.operations.queue import RetryQueue, WorkItem
from forex_monitor.operations.runbooks import (
    Runbook, RunbookManager, RunbookStep, StepState,
)
from forex_monitor.operations.slo import IndicatorEvent, ServiceLevelObjective, SloMonitor


NOW = datetime(2026, 5, 1, 10, tzinfo=timezone.utc)


class IncidentTests(unittest.TestCase):
    def test_incident_lifecycle_timeline_and_metrics(self):
        manager = IncidentManager()
        manager.create(Incident("inc", "quote delay", IncidentSeverity.SEV2, IncidentState.DETECTED, NOW, ("quotes",)))
        manager.update("inc", IncidentState.INVESTIGATING, NOW + timedelta(minutes=1), "operator", "triage", commander="lead")
        manager.update("inc", IncidentState.MITIGATING, NOW + timedelta(minutes=2), "operator", "failover")
        resolved = manager.update("inc", IncidentState.RESOLVED, NOW + timedelta(minutes=5), "operator", "stable")
        closed = manager.update("inc", IncidentState.CLOSED, NOW + timedelta(minutes=10), "lead", "reviewed")
        self.assertEqual(resolved.time_to_resolution, timedelta(minutes=5))
        self.assertEqual(closed.commander, "lead")
        self.assertEqual(manager.metrics()["meanTimeToResolutionSeconds"], 300.0)

    def test_invalid_transition_and_backward_notes_fail(self):
        manager = IncidentManager()
        manager.create(Incident("inc", "issue", IncidentSeverity.SEV3, IncidentState.DETECTED, NOW, ("service",)))
        with self.assertRaises(ValidationError):
            manager.update("inc", IncidentState.RESOLVED, NOW, "operator", "skip")
        with self.assertRaises(ValidationError):
            manager.note("inc", TimelineEntry(NOW - timedelta(seconds=1), "operator", "note", "old"))


class QueueTests(unittest.TestCase):
    def test_claim_retry_backoff_dead_letter_and_redrive(self):
        queue = RetryQueue(base_delay=timedelta(seconds=2), maximum_delay=timedelta(seconds=10))
        queue.enqueue(WorkItem("item", "deliver", {"id": 1}, NOW, maximum_attempts=2))
        claimed = queue.claim("worker", NOW, timedelta(seconds=5))[0]
        retried = queue.fail("item", "worker", NOW, "temporary")
        self.assertEqual(retried.available_at, NOW + timedelta(seconds=2))
        queue.claim("worker", NOW + timedelta(seconds=2), timedelta(seconds=5))
        self.assertIsNone(queue.fail("item", "worker", NOW + timedelta(seconds=2), "again"))
        self.assertEqual(len(queue.dead_letters()), 1)
        redriven = queue.redrive("item", NOW + timedelta(minutes=1))
        self.assertEqual(redriven.attempts, 0)

    def test_leases_enforce_ownership(self):
        queue = RetryQueue()
        queue.enqueue(WorkItem("item", "work", {}, NOW))
        queue.claim("a", NOW, timedelta(seconds=5))
        with self.assertRaises(StorageConflictError):
            queue.complete("item", "b")


class SloTests(unittest.TestCase):
    def test_slo_achievement_error_budget_and_burn(self):
        monitor = SloMonitor()
        monitor.add(ServiceLevelObjective("availability", "quotes", "success", Decimal("0.99"), timedelta(days=1)))
        for index in range(100):
            monitor.record(IndicatorEvent("quotes", "success", NOW - timedelta(minutes=index), index < 95))
        evaluation = monitor.evaluate("availability", NOW)
        self.assertEqual(evaluation.achieved, Decimal("0.95"))
        self.assertFalse(evaluation.met)
        self.assertEqual(evaluation.burn_rate, Decimal("5"))
        self.assertEqual(monitor.alerts(NOW)[0], evaluation)


class RunbookTests(unittest.TestCase):
    def test_ordered_steps_and_approval_gate(self):
        manager = RunbookManager()
        manager.register(Runbook("failover", "Provider failover", "quotes", (
            RunbookStep("verify", "Verify issue", "Check provider health"),
            RunbookStep("switch", "Switch provider", "Activate standby", requires_approval=True),
        )))
        execution = manager.start("exec", "failover", "operator", NOW, "inc")
        execution = manager.complete_step(
            "exec", "verify", "operator", NOW, NOW + timedelta(minutes=1),
            succeeded=True, evidence="provider unavailable",
        )
        self.assertEqual(execution.next_step.step_id, "switch")
        with self.assertRaises(ValidationError):
            manager.complete_step(
                "exec", "switch", "operator", NOW, NOW + timedelta(minutes=1),
                succeeded=True, evidence="switched",
            )
        execution = manager.complete_step(
            "exec", "switch", "operator", NOW, NOW + timedelta(minutes=1),
            succeeded=True, evidence="switched", approved_by="lead",
        )
        self.assertTrue(execution.complete)
        self.assertTrue(all(item.state is StepState.SUCCEEDED for item in execution.steps))


if __name__ == "__main__":
    unittest.main()
