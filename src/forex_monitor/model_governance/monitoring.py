"""Production performance gates, drift detection, and alert lifecycle."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from threading import RLock
from typing import Dict, Iterable, Mapping, Optional, Tuple

from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError
from forex_monitor.model_governance.models import AlertSeverity, MetricRequirement, ModelAlert, MonitoringObservation, number, required, utc


class ModelMonitor:
    def __init__(self) -> None:
        self._requirements: Dict[str, Tuple[MetricRequirement, ...]] = {}
        self._baselines: Dict[Tuple[str, str], Tuple[Decimal, ...]] = {}
        self._observations: Dict[str, MonitoringObservation] = {}
        self._deployment_observations: Dict[str, list[str]] = {}
        self._alerts: Dict[str, ModelAlert] = {}
        self._deployment_alerts: Dict[str, list[str]] = {}
        self._lock = RLock()

    def configure(
        self,
        deployment_id: str,
        *,
        requirements: Iterable[MetricRequirement],
        baseline_distributions: Mapping[str, Iterable[object]] = {},
    ) -> None:
        key = required(deployment_id, "deployment id")
        values = tuple(requirements)
        requirement_keys = [(item.name, item.segment) for item in values]
        if len(requirement_keys) != len(set(requirement_keys)):
            raise ValidationError("monitoring requirements are duplicated")
        baselines = {}
        for feature, bins in baseline_distributions.items():
            distribution = tuple(number(item, "baseline bin", non_negative=True) for item in bins)
            if not distribution or sum(distribution, Decimal("0")) <= 0:
                raise ValidationError("baseline distribution must contain positive mass")
            baselines[(key, feature)] = self._normalize(distribution)
        with self._lock:
            self._requirements[key] = values
            for baseline_key, distribution in baselines.items():
                self._baselines[baseline_key] = distribution

    def observe(
        self,
        observation: MonitoringObservation,
        *,
        drift_warning: object = "0.1",
        drift_critical: object = "0.25",
    ) -> Tuple[ModelAlert, ...]:
        if not isinstance(observation, MonitoringObservation):
            raise ValidationError("observation must be MonitoringObservation")
        warning = number(drift_warning, "drift warning", non_negative=True)
        critical = number(drift_critical, "drift critical", non_negative=True)
        if warning >= critical:
            raise ValidationError("drift warning must be below critical threshold")
        with self._lock:
            existing = self._observations.get(observation.observation_id)
            if existing is not None:
                if existing == observation:
                    return tuple(self._alerts[item] for item in self._deployment_alerts.get(observation.deployment_id, ()) if self._alerts[item].created_at == observation.observed_at)
                raise StorageConflictError("monitoring observation id already exists")
            alerts = []
            metric_values = dict(observation.metrics)
            for requirement in self._requirements.get(observation.deployment_id, ()):
                value = metric_values.get(requirement.name)
                if value is None:
                    alerts.append(self._new_alert(observation, "missing_metric", requirement.severity, requirement.name, Decimal("0"), requirement.threshold, "Required production metric is missing"))
                elif not requirement.satisfied_by(value):
                    alerts.append(self._new_alert(observation, "performance", requirement.severity, requirement.name, value, requirement.threshold, "Production metric breached its approved threshold"))
            for feature, distribution in observation.feature_distributions:
                baseline = self._baselines.get((observation.deployment_id, feature))
                if baseline is None:
                    continue
                current = self._normalize(distribution)
                if len(current) != len(baseline):
                    alerts.append(self._new_alert(observation, "schema_drift", AlertSeverity.CRITICAL, feature, Decimal(len(current)), Decimal(len(baseline)), "Feature distribution has a different number of bins"))
                    continue
                psi = self.population_stability_index(baseline, current)
                if psi >= critical:
                    alerts.append(self._new_alert(observation, "feature_drift", AlertSeverity.CRITICAL, feature, psi, critical, "Feature drift exceeded the critical threshold"))
                elif psi >= warning:
                    alerts.append(self._new_alert(observation, "feature_drift", AlertSeverity.WARNING, feature, psi, warning, "Feature drift exceeded the warning threshold"))
            self._observations[observation.observation_id] = observation
            self._deployment_observations.setdefault(observation.deployment_id, []).append(observation.observation_id)
            for alert in alerts:
                self._alerts[alert.alert_id] = alert
                self._deployment_alerts.setdefault(observation.deployment_id, []).append(alert.alert_id)
            return tuple(alerts)

    def resolve(self, alert_id: str, *, resolved_at: datetime) -> ModelAlert:
        instant = utc(resolved_at, "resolved at")
        with self._lock:
            current = self._alerts.get(required(alert_id, "alert id"))
            if current is None:
                raise NotFoundError("model alert does not exist")
            if current.resolved_at is not None:
                return current
            changed = replace(current, resolved_at=instant)
            self._alerts[current.alert_id] = changed
            return changed

    def observations(self, deployment_id: str) -> Tuple[MonitoringObservation, ...]:
        with self._lock:
            return tuple(self._observations[item] for item in self._deployment_observations.get(deployment_id, ()))

    def alerts(self, deployment_id: str, *, open_only: bool = False) -> Tuple[ModelAlert, ...]:
        with self._lock:
            values = [self._alerts[item] for item in self._deployment_alerts.get(deployment_id, ())]
            if open_only:
                values = [item for item in values if item.resolved_at is None]
            return tuple(sorted(values, key=lambda item: (item.created_at, item.alert_id)))

    @staticmethod
    def population_stability_index(expected: Iterable[object], actual: Iterable[object]) -> Decimal:
        expected_values = ModelMonitor._normalize(tuple(number(item, "expected bin", non_negative=True) for item in expected))
        actual_values = ModelMonitor._normalize(tuple(number(item, "actual bin", non_negative=True) for item in actual))
        if len(expected_values) != len(actual_values):
            raise ValidationError("PSI distributions must have equal bins")
        epsilon = Decimal("0.000001")
        from math import log
        total = Decimal("0")
        for expected_bin, actual_bin in zip(expected_values, actual_values):
            left = max(expected_bin, epsilon)
            right = max(actual_bin, epsilon)
            total += (right - left) * Decimal(str(log(float(right / left))))
        return total

    @staticmethod
    def _normalize(values: Iterable[Decimal]) -> Tuple[Decimal, ...]:
        items = tuple(values)
        total = sum(items, Decimal("0"))
        if not items or total <= 0:
            raise ValidationError("distribution must contain positive mass")
        return tuple(item / total for item in items)

    @staticmethod
    def _new_alert(observation: MonitoringObservation, alert_type: str, severity: AlertSeverity, metric: str, value: Decimal, threshold: Decimal, message: str) -> ModelAlert:
        alert_id = "%s:%s:%s" % (observation.observation_id, alert_type, metric)
        return ModelAlert(alert_id, observation.deployment_id, alert_type, severity, metric, value, threshold, observation.observed_at, message)
