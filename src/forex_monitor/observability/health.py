"""Composable liveness and readiness checks."""

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Sequence, Tuple


class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


@dataclass(frozen=True)
class HealthCheck:
    name: str
    status: HealthStatus
    message: str = ""


CheckFunction = Callable[[], HealthCheck]


def run_checks(checks: Sequence[CheckFunction]) -> Tuple[HealthCheck, ...]:
    results = []
    for check in checks:
        try:
            results.append(check())
        except Exception as error:  # defensive boundary for operational checks
            results.append(HealthCheck(check.__name__, HealthStatus.UNHEALTHY, str(error)))
    return tuple(results)


def overall_status(checks: Sequence[HealthCheck]) -> HealthStatus:
    statuses = {check.status for check in checks}
    if HealthStatus.UNHEALTHY in statuses:
        return HealthStatus.UNHEALTHY
    if HealthStatus.DEGRADED in statuses:
        return HealthStatus.DEGRADED
    return HealthStatus.HEALTHY
