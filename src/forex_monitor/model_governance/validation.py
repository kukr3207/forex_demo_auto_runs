"""Reproducible validation runs and metric-gate evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from math import sqrt
from threading import RLock
from typing import Dict, Iterable, Mapping, Optional, Sequence, Tuple

from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError
from forex_monitor.model_governance.models import ModelVersion, ValidationMetric, ValidationRun, number, required, utc


@dataclass(frozen=True)
class MetricFailure:
    name: str
    segment: Optional[str]
    value: Optional[Decimal]
    operator: str
    threshold: Decimal
    reason: str


@dataclass(frozen=True)
class ValidationOutcome:
    run: ValidationRun
    failures: Tuple[MetricFailure, ...]


class ModelValidator:
    def __init__(self) -> None:
        self._runs: Dict[str, ValidationOutcome] = {}
        self._model_runs: Dict[Tuple[str, str], list[str]] = {}
        self._lock = RLock()

    def evaluate(
        self,
        *,
        run_id: str,
        model: ModelVersion,
        dataset_id: str,
        metrics: Iterable[ValidationMetric],
        started_at: datetime,
        completed_at: datetime,
        validator: str = "automated",
    ) -> ValidationOutcome:
        values = tuple(metrics)
        lookup = {(item.name, item.segment): item for item in values}
        failures = []
        for requirement in model.requirements:
            metric = lookup.get((requirement.name, requirement.segment))
            if metric is None:
                failures.append(MetricFailure(requirement.name, requirement.segment, None, requirement.operator, requirement.threshold, "required metric is missing"))
            elif not requirement.satisfied_by(metric.value):
                failures.append(MetricFailure(requirement.name, requirement.segment, metric.value, requirement.operator, requirement.threshold, "metric does not satisfy requirement"))
        run = ValidationRun(run_id, model.model_id, model.version, dataset_id, started_at, completed_at, values, not failures, tuple(item.reason for item in failures), validator)
        outcome = ValidationOutcome(run, tuple(failures))
        with self._lock:
            existing = self._runs.get(run.run_id)
            if existing is not None:
                if existing == outcome:
                    return existing
                raise StorageConflictError("validation run id already exists")
            self._runs[run.run_id] = outcome
            self._model_runs.setdefault(model.key, []).append(run.run_id)
            return outcome

    def run(self, run_id: str) -> ValidationOutcome:
        with self._lock:
            value = self._runs.get(required(run_id, "validation run id"))
            if value is None:
                raise NotFoundError("validation run does not exist")
            return value

    def runs_for_model(self, model_id: str, version: str) -> Tuple[ValidationOutcome, ...]:
        with self._lock:
            return tuple(self._runs[item] for item in self._model_runs.get((model_id, version), ()))

    @staticmethod
    def regression_metrics(actual: Sequence[object], predicted: Sequence[object], *, segment: Optional[str] = None) -> Tuple[ValidationMetric, ...]:
        if len(actual) != len(predicted) or not actual:
            raise ValidationError("actual and predicted values must be non-empty and aligned")
        truth = [number(item, "actual value") for item in actual]
        estimates = [number(item, "predicted value") for item in predicted]
        errors = [estimate - value for value, estimate in zip(truth, estimates)]
        absolute = [abs(item) for item in errors]
        squared = [item * item for item in errors]
        mean = sum(truth, Decimal("0")) / len(truth)
        denominator = sum(((item - mean) ** 2 for item in truth), Decimal("0"))
        r_squared = Decimal("1") - sum(squared, Decimal("0")) / denominator if denominator else Decimal("0")
        return (
            ValidationMetric("mae", sum(absolute, Decimal("0")) / len(absolute), len(truth), segment),
            ValidationMetric("mse", sum(squared, Decimal("0")) / len(squared), len(truth), segment),
            ValidationMetric("rmse", Decimal(str(sqrt(float(sum(squared, Decimal("0")) / len(squared))))), len(truth), segment),
            ValidationMetric("bias", sum(errors, Decimal("0")) / len(errors), len(truth), segment),
            ValidationMetric("r_squared", r_squared, len(truth), segment),
        )

    @staticmethod
    def classification_metrics(actual: Sequence[bool], scores: Sequence[object], *, threshold: object = "0.5", segment: Optional[str] = None) -> Tuple[ValidationMetric, ...]:
        if len(actual) != len(scores) or not actual:
            raise ValidationError("labels and scores must be non-empty and aligned")
        cutoff = number(threshold, "classification threshold")
        values = [number(item, "classification score") for item in scores]
        if any(item < 0 or item > 1 for item in values):
            raise ValidationError("classification scores must be between zero and one")
        predicted = [item >= cutoff for item in values]
        tp = sum(1 for truth, estimate in zip(actual, predicted) if truth and estimate)
        tn = sum(1 for truth, estimate in zip(actual, predicted) if not truth and not estimate)
        fp = sum(1 for truth, estimate in zip(actual, predicted) if not truth and estimate)
        fn = sum(1 for truth, estimate in zip(actual, predicted) if truth and not estimate)
        precision = Decimal(tp) / Decimal(tp + fp) if tp + fp else Decimal("0")
        recall = Decimal(tp) / Decimal(tp + fn) if tp + fn else Decimal("0")
        accuracy = Decimal(tp + tn) / Decimal(len(actual))
        f1 = Decimal("2") * precision * recall / (precision + recall) if precision + recall else Decimal("0")
        brier = sum(((score - (Decimal("1") if truth else Decimal("0"))) ** 2 for truth, score in zip(actual, values)), Decimal("0")) / len(actual)
        return (
            ValidationMetric("accuracy", accuracy, len(actual), segment),
            ValidationMetric("precision", precision, len(actual), segment),
            ValidationMetric("recall", recall, len(actual), segment),
            ValidationMetric("f1", f1, len(actual), segment),
            ValidationMetric("brier", brier, len(actual), segment),
        )
