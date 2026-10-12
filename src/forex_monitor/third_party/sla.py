"""Service-level observations, evaluation, and credit calculation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from threading import RLock
from typing import Dict, Iterable, Optional, Tuple

from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError
from forex_monitor.third_party.models import MetricKind, ServiceContract, ServiceLevel, amount, required, utc


@dataclass(frozen=True)
class Measurement:
    measurement_id: str
    contract_id: str
    metric_id: str
    period_start: datetime
    period_end: datetime
    value: Decimal
    sample_count: int
    source: str
    excluded_seconds: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "measurement_id", required(self.measurement_id, "measurement id"))
        object.__setattr__(self, "contract_id", required(self.contract_id, "contract id"))
        object.__setattr__(self, "metric_id", required(self.metric_id, "metric id"))
        start = utc(self.period_start, "measurement start")
        end = utc(self.period_end, "measurement end")
        if end <= start:
            raise ValidationError("measurement end must follow its start")
        object.__setattr__(self, "period_start", start)
        object.__setattr__(self, "period_end", end)
        object.__setattr__(self, "value", amount(self.value, "measurement value"))
        if not isinstance(self.sample_count, int) or isinstance(self.sample_count, bool) or self.sample_count < 1:
            raise ValidationError("measurement sample count must be positive")
        object.__setattr__(self, "source", required(self.source, "measurement source"))
        if self.excluded_seconds < 0 or self.excluded_seconds >= int((end - start).total_seconds()):
            raise ValidationError("excluded seconds must fit within the measurement period")


@dataclass(frozen=True)
class MetricEvaluation:
    contract_id: str
    metric_id: str
    period_start: datetime
    period_end: datetime
    observed: Decimal
    target: Decimal
    met: bool
    measurement_count: int
    sample_count: int


@dataclass(frozen=True)
class ServiceCredit:
    contract_id: str
    metric_id: str
    period_start: datetime
    period_end: datetime
    currency: str
    eligible_amount: Decimal
    credit_amount: Decimal
    reason: str


class ServiceLevelManager:
    def __init__(self) -> None:
        self._measurements: Dict[str, Measurement] = {}
        self._contracts: Dict[str, ServiceContract] = {}
        self._lock = RLock()

    def register_contract(self, contract: ServiceContract) -> ServiceContract:
        if not isinstance(contract, ServiceContract):
            raise ValidationError("contract must be ServiceContract")
        with self._lock:
            existing = self._contracts.get(contract.contract_id)
            if existing is not None and existing != contract:
                raise StorageConflictError("contract definition changed")
            self._contracts[contract.contract_id] = contract
            return contract

    def record(self, measurement: Measurement) -> Measurement:
        if not isinstance(measurement, Measurement):
            raise ValidationError("measurement must be Measurement")
        with self._lock:
            contract = self._require_contract(measurement.contract_id)
            self._metric(contract, measurement.metric_id)
            if measurement.period_start.date() < contract.starts_on or measurement.period_end.date() > contract.ends_on:
                raise ValidationError("measurement is outside the contract term")
            existing = self._measurements.get(measurement.measurement_id)
            if existing is not None:
                if existing == measurement:
                    return existing
                raise StorageConflictError("measurement id already exists")
            for item in self._measurements.values():
                if item.contract_id != measurement.contract_id or item.metric_id != measurement.metric_id:
                    continue
                if measurement.period_start < item.period_end and item.period_start < measurement.period_end:
                    raise ValidationError("measurement periods overlap")
            self._measurements[measurement.measurement_id] = measurement
            return measurement

    def observations(
        self,
        contract_id: str,
        metric_id: str,
        *,
        start: datetime,
        end: datetime,
    ) -> Tuple[Measurement, ...]:
        period_start = utc(start, "period start")
        period_end = utc(end, "period end")
        if period_end <= period_start:
            raise ValidationError("period end must follow its start")
        with self._lock:
            contract = self._require_contract(contract_id)
            self._metric(contract, metric_id)
            values = tuple(
                item
                for item in self._measurements.values()
                if item.contract_id == contract_id
                and item.metric_id == metric_id
                and item.period_start >= period_start
                and item.period_end <= period_end
            )
        return tuple(sorted(values, key=lambda item: (item.period_start, item.measurement_id)))

    def evaluate(
        self,
        contract_id: str,
        metric_id: str,
        *,
        start: datetime,
        end: datetime,
    ) -> MetricEvaluation:
        contract = self._require_contract(contract_id)
        metric = self._metric(contract, metric_id)
        observations = self.observations(contract_id, metric_id, start=start, end=end)
        if not observations:
            raise ValidationError("service level period has no measurements")
        sample_count = sum(item.sample_count for item in observations)
        if metric.kind in {MetricKind.AVAILABILITY, MetricKind.ERROR_RATE, MetricKind.DELIVERY}:
            observed = sum((item.value * item.sample_count for item in observations), Decimal("0")) / sample_count
        elif metric.kind == MetricKind.LATENCY:
            observed = self._weighted_percentile(observations, Decimal("0.95"))
        else:
            observed = sum((item.value for item in observations), Decimal("0")) / len(observations)
        observed = observed.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        return MetricEvaluation(
            contract_id,
            metric_id,
            utc(start, "period start"),
            utc(end, "period end"),
            observed,
            metric.target,
            metric.met_by(observed),
            len(observations),
            sample_count,
        )

    def evaluate_contract(
        self,
        contract_id: str,
        *,
        start: datetime,
        end: datetime,
    ) -> Tuple[MetricEvaluation, ...]:
        contract = self._require_contract(contract_id)
        results: list[MetricEvaluation] = []
        for metric in contract.service_levels:
            try:
                results.append(self.evaluate(contract_id, metric.metric_id, start=start, end=end))
            except ValidationError:
                continue
        return tuple(results)

    def credit(
        self,
        contract_id: str,
        metric_id: str,
        *,
        start: datetime,
        end: datetime,
        eligible_amount: Decimal,
    ) -> ServiceCredit:
        contract = self._require_contract(contract_id)
        metric = self._metric(contract, metric_id)
        eligible = amount(eligible_amount, "eligible amount")
        evaluation = self.evaluate(contract_id, metric_id, start=start, end=end)
        if evaluation.met or metric.credit_rate == 0:
            credit_amount = Decimal("0")
            reason = "service level met"
        else:
            base_credit = eligible * metric.credit_rate
            cap = eligible * metric.credit_cap if metric.credit_cap else base_credit
            credit_amount = min(base_credit, cap).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            reason = "%s observed %s against target %s" % (metric.name, evaluation.observed, metric.target)
        return ServiceCredit(
            contract_id,
            metric_id,
            evaluation.period_start,
            evaluation.period_end,
            contract.currency,
            eligible,
            credit_amount,
            reason,
        )

    def breaches(
        self,
        contract_id: str,
        *,
        as_of: datetime,
    ) -> Tuple[MetricEvaluation, ...]:
        contract = self._require_contract(contract_id)
        end = utc(as_of, "as of")
        results: list[MetricEvaluation] = []
        for metric in contract.service_levels:
            start = end - timedelta(days=metric.window_days)
            try:
                evaluation = self.evaluate(contract_id, metric.metric_id, start=start, end=end)
            except ValidationError:
                continue
            if not evaluation.met:
                results.append(evaluation)
        return tuple(results)

    def coverage(self, contract_id: str, metric_id: str, *, start: datetime, end: datetime) -> Decimal:
        period_start = utc(start, "period start")
        period_end = utc(end, "period end")
        if period_end <= period_start:
            raise ValidationError("period end must follow its start")
        observations = self.observations(contract_id, metric_id, start=period_start, end=period_end)
        covered = sum(
            (item.period_end - item.period_start).total_seconds() - item.excluded_seconds
            for item in observations
        )
        possible = (period_end - period_start).total_seconds()
        return (Decimal(str(covered)) / Decimal(str(possible))).quantize(Decimal("0.000001"))

    def _require_contract(self, contract_id: str) -> ServiceContract:
        try:
            return self._contracts[required(contract_id, "contract id")]
        except KeyError as exc:
            raise NotFoundError("service-level contract does not exist") from exc

    @staticmethod
    def _metric(contract: ServiceContract, metric_id: str) -> ServiceLevel:
        target = required(metric_id, "metric id")
        try:
            return next(item for item in contract.service_levels if item.metric_id == target)
        except StopIteration as exc:
            raise NotFoundError("contract metric does not exist") from exc

    @staticmethod
    def _weighted_percentile(values: Iterable[Measurement], percentile: Decimal) -> Decimal:
        ordered = sorted(values, key=lambda item: item.value)
        total = sum(item.sample_count for item in ordered)
        threshold = Decimal(total) * percentile
        cumulative = 0
        for item in ordered:
            cumulative += item.sample_count
            if Decimal(cumulative) >= threshold:
                return item.value
        return ordered[-1].value
