"""Unified operational snapshot service."""

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from forex_monitor.observability.audit import AuditLog
from forex_monitor.observability.diagnostics import runtime_diagnostics
from forex_monitor.observability.health import CheckFunction, overall_status, run_checks
from forex_monitor.observability.metrics import MetricRegistry


@dataclass
class ObservabilityService:
    metrics: MetricRegistry
    audit: AuditLog

    def snapshot(
        self, database_path: Path, checks: Sequence[CheckFunction] = ()
    ) -> Mapping[str, object]:
        health = run_checks(checks)
        return {
            "status": overall_status(health).value,
            "checks": [
                {"name": item.name, "status": item.status.value, "message": item.message}
                for item in health
            ],
            "metrics": self.metrics.snapshot(),
            "recentEvents": [event.as_dict() for event in self.audit.list(limit=20)],
            "runtime": runtime_diagnostics(database_path),
        }
