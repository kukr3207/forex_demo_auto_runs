"""Transactional rule evaluation and reliable pending-alert delivery."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Iterable, Mapping, Optional, Sequence, Tuple

from forex_monitor.alerts.evaluator import Evaluation, RuleEvaluator, render_alert_message
from forex_monitor.alerts.notifications import NotificationResult, Notifier
from forex_monitor.ids import IdentifierFactory, uuid_hex
from forex_monitor.models import Alert, AlertRule, AlertStatus, Quote, ensure_utc
from forex_monitor.storage import AlertRepository, Database


@dataclass(frozen=True)
class DeliverySummary:
    attempted: int
    delivered: int
    failed: int
    results: Tuple[Tuple[str, NotificationResult], ...]

    def as_dict(self) -> Mapping[str, object]:
        return {
            "attempted": self.attempted,
            "delivered": self.delivered,
            "failed": self.failed,
            "results": [
                {
                    "alertId": alert_id,
                    "notifier": result.notifier,
                    "delivered": result.delivered,
                    "attemptedAt": result.attempted_at.isoformat(),
                    "externalId": result.external_id,
                    "error": result.error,
                }
                for alert_id, result in self.results
            ],
        }


@dataclass
class AlertService:
    database: Database
    repository: AlertRepository
    notifier: Notifier
    evaluator: RuleEvaluator = field(default_factory=RuleEvaluator)
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))
    identifiers: IdentifierFactory = uuid_hex

    def evaluate_quote(
        self,
        quote: Quote,
        *,
        previous: Optional[Quote] = None,
    ) -> Tuple[Alert, ...]:
        rules = self.repository.rules_for(quote.symbol)
        return self.evaluate_metrics(
            quote.symbol,
            {
                "bid": quote.bid,
                "ask": quote.ask,
                "mid": quote.mid,
                "spread": quote.spread,
                "volume": quote.volume or Decimal("0"),
            },
            previous_metrics=(
                {
                    "bid": previous.bid,
                    "ask": previous.ask,
                    "mid": previous.mid,
                    "spread": previous.spread,
                    "volume": previous.volume or Decimal("0"),
                }
                if previous
                else {}
            ),
            rules=rules,
        )

    def evaluate_metrics(
        self,
        symbol: str,
        metrics: Mapping[str, object],
        *,
        previous_metrics: Mapping[str, object] = {},
        rules: Optional[Sequence[AlertRule]] = None,
    ) -> Tuple[Alert, ...]:
        evaluated_at = ensure_utc(self.clock(), "alert evaluation time")
        active_rules = tuple(rules) if rules is not None else self.repository.rules_for(symbol)
        triggered = []
        for rule in active_rules:
            if rule.symbol != symbol:
                continue
            if rule.metric not in metrics:
                continue
            evaluation = self.evaluator.evaluate(
                rule,
                metrics[rule.metric],
                previous_value=previous_metrics.get(rule.metric),
                evaluated_at=evaluated_at,
            )
            if not evaluation.should_trigger:
                continue
            alert = self._alert(rule, evaluation)
            updated_rule = rule.with_trigger(evaluated_at)
            with self.database.transaction(write=True) as transaction:
                self.repository.add_alert(alert, transaction=transaction)
                self.repository.save_rule(updated_rule, transaction=transaction)
            triggered.append(alert)
        return tuple(triggered)

    def deliver_pending(self, *, limit: int = 100) -> DeliverySummary:
        pending = self.repository.pending(limit)
        results = []
        delivered = 0
        failed = 0
        for alert in pending:
            result = self.notifier.notify(alert)
            results.append((alert.id, result))
            if result.delivered:
                delivered += 1
                updated = replace(
                    alert,
                    status=AlertStatus.DELIVERED,
                    delivered_at=result.attempted_at,
                    delivery_error=None,
                )
            else:
                failed += 1
                updated = replace(
                    alert,
                    status=AlertStatus.FAILED,
                    delivery_error=(result.error or "notification failed")[:1000],
                )
            self.repository.update_alert(updated)
        return DeliverySummary(len(pending), delivered, failed, tuple(results))

    def _alert(self, rule: AlertRule, evaluation: Evaluation) -> Alert:
        return Alert(
            id=self.identifiers(),
            rule_id=rule.id,
            symbol=rule.symbol,
            metric=rule.metric,
            observed_value=evaluation.current_value,
            threshold=rule.threshold,
            operator=rule.operator,
            triggered_at=evaluation.evaluated_at,
            message=render_alert_message(rule, evaluation),
        )
