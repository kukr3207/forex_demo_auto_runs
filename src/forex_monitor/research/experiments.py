"""Experiment and trial registry with reproducible parameter fingerprints."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from enum import Enum
from hashlib import sha256
import json
from typing import Dict, Mapping, Optional, Sequence, Tuple

from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError
from forex_monitor.models import as_decimal, ensure_utc


class TrialState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Trial:
    trial_id: str
    parameters: Mapping[str, object]
    state: TrialState = TrialState.PENDING
    metrics: Mapping[str, Decimal] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    error: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.trial_id.strip():
            raise ValidationError("trial id cannot be blank")
        parameters = {}
        for name, value in self.parameters.items():
            if not name.strip() or not isinstance(value, (str, int, float, bool, type(None))):
                raise ValidationError("trial parameters must be named JSON scalars")
            parameters[name.strip()] = value
        metrics = {name: as_decimal(value, name) for name, value in (self.metrics or {}).items()}
        started = ensure_utc(self.started_at, "trial start") if self.started_at else None
        finished = ensure_utc(self.finished_at, "trial finish") if self.finished_at else None
        if finished and not started:
            raise ValidationError("finished trial requires a start time")
        if finished and finished < started:
            raise ValidationError("trial finish cannot precede start")
        object.__setattr__(self, "parameters", parameters)
        object.__setattr__(self, "metrics", metrics)
        object.__setattr__(self, "state", self.state if isinstance(self.state, TrialState) else TrialState(self.state))
        object.__setattr__(self, "started_at", started)
        object.__setattr__(self, "finished_at", finished)

    @property
    def fingerprint(self) -> str:
        return sha256(json.dumps(self.parameters, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Experiment:
    experiment_id: str
    name: str
    dataset_digest: str
    code_revision: str
    objective: str
    maximize: bool = True
    trials: Tuple[Trial, ...] = ()

    def __post_init__(self) -> None:
        if not all((self.experiment_id.strip(), self.name.strip(), self.dataset_digest.strip(), self.code_revision.strip(), self.objective.strip())):
            raise ValidationError("experiment fields cannot be blank")
        fingerprints = [trial.fingerprint for trial in self.trials]
        if len(set(fingerprints)) != len(fingerprints):
            raise ValidationError("experiment contains duplicate parameter trials")

    @property
    def best(self) -> Optional[Trial]:
        candidates = [trial for trial in self.trials if trial.state is TrialState.SUCCEEDED and self.objective in trial.metrics]
        if not candidates:
            return None
        return sorted(candidates, key=lambda item: item.metrics[self.objective], reverse=self.maximize)[0]


class ExperimentRegistry:
    def __init__(self) -> None:
        self._experiments: Dict[str, Experiment] = {}

    def create(self, experiment: Experiment) -> Experiment:
        if experiment.experiment_id in self._experiments:
            raise StorageConflictError("experiment id already exists")
        self._experiments[experiment.experiment_id] = experiment
        return experiment

    def get(self, experiment_id: str) -> Experiment:
        try:
            return self._experiments[experiment_id]
        except KeyError as exc:
            raise NotFoundError("experiment was not found") from exc

    def add_trial(self, experiment_id: str, trial: Trial) -> Experiment:
        experiment = self.get(experiment_id)
        if any(item.trial_id == trial.trial_id for item in experiment.trials):
            existing = next(item for item in experiment.trials if item.trial_id == trial.trial_id)
            if existing == trial:
                return experiment
            raise StorageConflictError("trial id already exists")
        if any(item.fingerprint == trial.fingerprint for item in experiment.trials):
            raise StorageConflictError("trial parameters were already evaluated")
        changed = replace(experiment, trials=experiment.trials + (trial,))
        self._experiments[experiment_id] = changed
        return changed

    def start(self, experiment_id: str, trial_id: str, at: datetime) -> Trial:
        return self._update(experiment_id, trial_id, TrialState.RUNNING, at, None, None)

    def complete(self, experiment_id: str, trial_id: str, at: datetime, metrics: Mapping[str, object]) -> Trial:
        parsed = {name: as_decimal(value, name) for name, value in metrics.items()}
        return self._update(experiment_id, trial_id, TrialState.SUCCEEDED, at, parsed, None)

    def fail(self, experiment_id: str, trial_id: str, at: datetime, error: str) -> Trial:
        if not error.strip():
            raise ValidationError("failed trial requires an error")
        return self._update(experiment_id, trial_id, TrialState.FAILED, at, None, error.strip())

    def _update(
        self,
        experiment_id: str,
        trial_id: str,
        state: TrialState,
        at: datetime,
        metrics: Optional[Mapping[str, Decimal]],
        error: Optional[str],
    ) -> Trial:
        experiment = self.get(experiment_id)
        timestamp = ensure_utc(at, "trial event time")
        trials = list(experiment.trials)
        for index, trial in enumerate(trials):
            if trial.trial_id != trial_id:
                continue
            if state is TrialState.RUNNING:
                if trial.state is not TrialState.PENDING:
                    raise ValidationError("only pending trials can start")
                changed = replace(trial, state=state, started_at=timestamp)
            else:
                if trial.state is not TrialState.RUNNING:
                    raise ValidationError("only running trials can finish")
                changed = replace(trial, state=state, finished_at=timestamp, metrics=metrics or {}, error=error)
            trials[index] = changed
            self._experiments[experiment_id] = replace(experiment, trials=tuple(trials))
            return changed
        raise NotFoundError("trial was not found")
