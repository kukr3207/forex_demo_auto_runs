"""Pure alert comparison and cooldown evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Mapping, Optional

from forex_monitor.errors import AlertEvaluationError
from forex_monitor.models import AlertOperator, AlertRule, as_decimal, ensure_utc


@dataclass(frozen=True)
class Evaluation:
    """Complete explanation of one alert-rule evaluation."""

    rule_id: str
    matched: bool
    suppressed: bool
    current_value: Decimal
    previous_value: Optional[Decimal]
    threshold: Decimal
    operator: AlertOperator
    evaluated_at: datetime
    reason: str

    @property
    def should_trigger(self) -> bool:
        return self.matched and not self.suppressed

    def as_dict(self) -> Mapping[str, object]:
        return {
            "ruleId": self.rule_id,
            "matched": self.matched,
            "suppressed": self.suppressed,
            "shouldTrigger": self.should_trigger,
            "currentValue": str(self.current_value),
            "previousValue": (
                str(self.previous_value) if self.previous_value is not None else None
            ),
            "threshold": str(self.threshold),
            "operator": self.operator.value,
            "evaluatedAt": self.evaluated_at.isoformat(),
            "reason": self.reason,
        }


class RuleEvaluator:
    """Evaluate threshold and crossing rules without side effects."""

    def evaluate(
        self,
        rule: AlertRule,
        current_value: object,
        *,
        evaluated_at: datetime,
        previous_value: Optional[object] = None,
    ) -> Evaluation:
        now = ensure_utc(evaluated_at, "evaluated at")
        current = as_decimal(current_value, "current metric value")
        previous = (
            as_decimal(previous_value, "previous metric value")
            if previous_value is not None
            else None
        )
        if not rule.enabled:
            return self._result(rule, current, previous, now, False, True, "rule is disabled")
        matched, reason = self._compare(rule.operator, current, previous, rule.threshold)
        suppressed = False
        if matched and rule.last_triggered_at is not None:
            eligible_at = rule.last_triggered_at + timedelta(seconds=rule.cooldown_seconds)
            if now < eligible_at:
                suppressed = True
                reason = f"matched but cooldown remains until {eligible_at.isoformat()}"
        return self._result(rule, current, previous, now, matched, suppressed, reason)

    def _compare(
        self,
        operator: AlertOperator,
        current: Decimal,
        previous: Optional[Decimal],
        threshold: Decimal,
    ) -> tuple[bool, str]:
        if operator is AlertOperator.GREATER_THAN:
            return current > threshold, f"{current} is {'above' if current > threshold else 'not above'} {threshold}"
        if operator is AlertOperator.GREATER_OR_EQUAL:
            matched = current >= threshold
            return matched, f"{current} is {'at or above' if matched else 'below'} {threshold}"
        if operator is AlertOperator.LESS_THAN:
            matched = current < threshold
            return matched, f"{current} is {'below' if matched else 'not below'} {threshold}"
        if operator is AlertOperator.LESS_OR_EQUAL:
            matched = current <= threshold
            return matched, f"{current} is {'at or below' if matched else 'above'} {threshold}"
        if previous is None:
            return False, "crossing operators require a previous value"
        if operator is AlertOperator.CROSSES_ABOVE:
            matched = previous <= threshold < current
            return matched, (
                f"value crossed above {threshold}"
                if matched
                else f"value did not cross above {threshold}"
            )
        if operator is AlertOperator.CROSSES_BELOW:
            matched = previous >= threshold > current
            return matched, (
                f"value crossed below {threshold}"
                if matched
                else f"value did not cross below {threshold}"
            )
        raise AlertEvaluationError("alert operator is not supported")

    @staticmethod
    def _result(
        rule: AlertRule,
        current: Decimal,
        previous: Optional[Decimal],
        now: datetime,
        matched: bool,
        suppressed: bool,
        reason: str,
    ) -> Evaluation:
        return Evaluation(
            rule_id=rule.id,
            matched=matched,
            suppressed=suppressed,
            current_value=current,
            previous_value=previous,
            threshold=rule.threshold,
            operator=rule.operator,
            evaluated_at=now,
            reason=reason,
        )


def render_alert_message(rule: AlertRule, evaluation: Evaluation) -> str:
    """Build a stable human-readable message for a matched rule."""

    if not evaluation.should_trigger:
        raise AlertEvaluationError("cannot render an alert for a non-triggering evaluation")
    return (
        f"{rule.name}: {rule.symbol} {rule.metric} is {evaluation.current_value}; "
        f"condition {rule.operator.value} {rule.threshold} matched."
    )

