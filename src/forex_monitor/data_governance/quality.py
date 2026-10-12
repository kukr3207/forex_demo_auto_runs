"""Executable row-level and dataset-level quality contracts."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from decimal import Decimal
from threading import RLock
from typing import Any, Dict, Iterable, Mapping, Sequence, Tuple

from forex_monitor.data_governance.models import CheckType, QualityCheck, QualityResult, number, utc
from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError


class DataQualityService:
    def __init__(self) -> None:
        self._checks: Dict[str, QualityCheck] = {}
        self._results: Dict[Tuple[str, str], QualityResult] = {}
        self._lock = RLock()

    def add_check(self, check: QualityCheck) -> QualityCheck:
        if not isinstance(check, QualityCheck):
            raise ValidationError("check must be QualityCheck")
        with self._lock:
            existing = self._checks.get(check.check_id)
            if existing is not None:
                if existing == check:
                    return existing
                raise StorageConflictError("quality check id already exists")
            self._checks[check.check_id] = check
            return check

    def run(self, run_id: str, dataset_id: str, rows: Sequence[Mapping[str, Any]], *, checked_at: datetime) -> Tuple[QualityResult, ...]:
        instant = utc(checked_at, "checked at")
        with self._lock:
            checks = [item for item in self._checks.values() if item.dataset_id == dataset_id and item.enabled]
        results = []
        for check in sorted(checks, key=lambda item: item.check_id):
            key = (run_id, check.check_id)
            with self._lock:
                existing = self._results.get(key)
            if existing is not None:
                results.append(existing)
                continue
            result = self._evaluate(run_id, check, rows, instant)
            with self._lock:
                self._results[key] = result
            results.append(result)
        return tuple(results)

    def history(self, check_id: str) -> Tuple[QualityResult, ...]:
        with self._lock:
            if check_id not in self._checks:
                raise NotFoundError("quality check does not exist")
            values = [item for (_, item_id), item in self._results.items() if item_id == check_id]
            return tuple(sorted(values, key=lambda item: item.checked_at))

    def _evaluate(self, run_id: str, check: QualityCheck, rows: Sequence[Mapping[str, Any]], at: datetime) -> QualityResult:
        parameters = dict(check.parameters)
        failed = []
        if check.check_type == CheckType.ROW_COUNT:
            minimum = int(parameters.get("minimum", "0"))
            maximum = int(parameters.get("maximum", str(2 ** 63 - 1)))
            passed = minimum <= len(rows) <= maximum
            observed = Decimal(len(rows))
            expected = Decimal(minimum)
            return QualityResult(run_id, check.check_id, check.dataset_id, at, passed, observed, expected, (), "row count outside bounds" if not passed else "")
        if check.check_type == CheckType.FRESHNESS:
            if not check.field:
                raise ValidationError("freshness check requires a field")
            maximum_age = timedelta(seconds=int(parameters.get("max_age_seconds", "0")))
            timestamps = [row.get(check.field) for row in rows if row.get(check.field) is not None]
            latest = max(timestamps) if timestamps else None
            passed = isinstance(latest, datetime) and latest.tzinfo is not None and at - latest <= maximum_age
            observed = Decimal(str((at - latest).total_seconds())) if isinstance(latest, datetime) and latest.tzinfo else Decimal("Infinity")
            return QualityResult(run_id, check.check_id, check.dataset_id, at, passed, observed, Decimal(str(maximum_age.total_seconds())), (), "dataset is stale" if not passed else "")
        if not check.field:
            raise ValidationError("row-level quality check requires a field")
        seen = set()
        for index, row in enumerate(rows):
            value = row.get(check.field)
            valid = True
            if check.check_type == CheckType.NOT_NULL:
                valid = value is not None
            elif check.check_type == CheckType.UNIQUE:
                valid = value is not None and value not in seen
                seen.add(value)
            elif check.check_type == CheckType.RANGE:
                valid = value is not None
                if valid and "minimum" in parameters:
                    valid = number(value, check.field) >= number(parameters["minimum"], "minimum")
                if valid and "maximum" in parameters:
                    valid = number(value, check.field) <= number(parameters["maximum"], "maximum")
            elif check.check_type == CheckType.ALLOWED_VALUES:
                allowed = set(parameters.get("values", "").split("|"))
                valid = str(value) in allowed
            elif check.check_type == CheckType.REGEX:
                valid = value is not None and re.fullmatch(parameters.get("pattern", ""), str(value)) is not None
            if not valid:
                failed.append(index)
        success_rate = Decimal(len(rows) - len(failed)) / Decimal(len(rows)) if rows else Decimal("0")
        passed = success_rate >= check.threshold
        return QualityResult(run_id, check.check_id, check.dataset_id, at, passed, success_rate, check.threshold, tuple(failed), "%d rows failed" % len(failed) if failed else "")
