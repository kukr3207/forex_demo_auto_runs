"""Trade surveillance for layering, wash activity, and rapid reversals."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Dict, Iterable, Mapping, Optional, Sequence, Tuple

from forex_monitor.execution.fills import Fill
from forex_monitor.execution.orders import Order, OrderSide, OrderState
from forex_monitor.models import as_decimal, ensure_utc


class AlertType(str, Enum):
    WASH_TRADE = "wash_trade"
    RAPID_REVERSAL = "rapid_reversal"
    LAYERING = "layering"
    EXCESSIVE_CANCELLATION = "excessive_cancellation"
    OFF_MARKET_EXECUTION = "off_market_execution"


@dataclass(frozen=True)
class SurveillanceAlert:
    alert_type: AlertType
    account_id: str
    symbol: str
    observed_at: datetime
    severity: str
    references: Tuple[str, ...]
    description: str
    score: Decimal

    def as_dict(self) -> Mapping[str, object]:
        return {
            "type": self.alert_type.value,
            "accountId": self.account_id,
            "symbol": self.symbol,
            "observedAt": self.observed_at.isoformat(),
            "severity": self.severity,
            "references": self.references,
            "description": self.description,
            "score": str(self.score),
        }


class SurveillanceEngine:
    def __init__(
        self,
        *,
        reversal_window: timedelta = timedelta(minutes=5),
        cancellation_ratio: object = Decimal("0.8"),
        price_deviation_bps: object = Decimal("20"),
    ) -> None:
        self.reversal_window = reversal_window
        self.cancellation_ratio = as_decimal(cancellation_ratio, "cancellation ratio")
        self.price_deviation_bps = as_decimal(price_deviation_bps, "price deviation", positive=True)

    def fills(self, fills: Sequence[Fill], accounts: Mapping[str, str]) -> Tuple[SurveillanceAlert, ...]:
        alerts = []
        ordered = sorted(fills, key=lambda item: (item.executed_at, item.fill_id))
        for index, current in enumerate(ordered):
            account = accounts.get(current.order_id, "unknown")
            for prior in ordered[max(0, index - 100):index]:
                if current.symbol != prior.symbol:
                    continue
                prior_account = accounts.get(prior.order_id, "unknown")
                if account != prior_account or current.side is prior.side:
                    continue
                elapsed = current.executed_at - prior.executed_at
                if elapsed > self.reversal_window:
                    continue
                common = min(current.quantity, prior.quantity)
                ratio = common / max(current.quantity, prior.quantity)
                alert_type = AlertType.WASH_TRADE if current.price == prior.price and ratio >= Decimal("0.9") else AlertType.RAPID_REVERSAL
                score = ratio * max(Decimal("0"), Decimal("1") - Decimal(str(elapsed.total_seconds())) / Decimal(str(self.reversal_window.total_seconds())))
                alerts.append(SurveillanceAlert(
                    alert_type, account, current.symbol, current.executed_at,
                    "high" if score >= Decimal("0.75") else "medium",
                    (prior.fill_id, current.fill_id),
                    "opposing executions occurred within the configured reversal window",
                    score,
                ))
        return tuple(alerts)

    def order_activity(self, orders: Sequence[Order]) -> Tuple[SurveillanceAlert, ...]:
        groups: Dict[Tuple[str, str], list[Order]] = {}
        for order in orders:
            groups.setdefault((order.account_id or "unknown", order.symbol), []).append(order)
        alerts = []
        for (account, symbol), values in groups.items():
            cancelled = [item for item in values if item.state in {OrderState.CANCELLED, OrderState.EXPIRED}]
            ratio = Decimal(len(cancelled)) / Decimal(len(values))
            if len(values) >= 5 and ratio >= self.cancellation_ratio:
                alerts.append(SurveillanceAlert(
                    AlertType.EXCESSIVE_CANCELLATION, account, symbol,
                    max(item.updated_at for item in values),
                    "medium", tuple(item.order_id for item in cancelled),
                    "account cancellation ratio exceeded the surveillance threshold", ratio,
                ))
            sides = {item.side for item in values if not item.state.terminal}
            if len(values) >= 4 and len(sides) > 1:
                open_orders = [item for item in values if not item.state.terminal]
                score = min(Decimal(len(open_orders)) / Decimal("10"), Decimal("1"))
                alerts.append(SurveillanceAlert(
                    AlertType.LAYERING, account, symbol,
                    max(item.updated_at for item in values),
                    "medium", tuple(item.order_id for item in open_orders),
                    "opposing open orders may create a misleading depth signal", score,
                ))
        return tuple(alerts)

    def off_market(self, fill: Fill, benchmark: object, account_id: str) -> Optional[SurveillanceAlert]:
        reference = as_decimal(benchmark, "market benchmark", positive=True)
        deviation = abs(fill.price - reference) / reference * Decimal("10000")
        if deviation <= self.price_deviation_bps:
            return None
        score = min(deviation / self.price_deviation_bps, Decimal("5")) / Decimal("5")
        return SurveillanceAlert(
            AlertType.OFF_MARKET_EXECUTION, account_id, fill.symbol, fill.executed_at,
            "high" if deviation >= self.price_deviation_bps * Decimal("2") else "medium",
            (fill.fill_id,), "execution price deviated from the supplied market benchmark", score,
        )
