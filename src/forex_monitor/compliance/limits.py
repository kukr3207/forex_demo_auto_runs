"""Hierarchical pre-trade compliance and concentration limits."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Dict, Mapping, Optional, Sequence, Tuple

from forex_monitor.errors import ValidationError
from forex_monitor.models import as_decimal, normalize_symbol


class LimitMetric(str, Enum):
    ORDER_NOTIONAL = "order_notional"
    DAILY_NOTIONAL = "daily_notional"
    OPEN_NOTIONAL = "open_notional"
    NET_POSITION = "net_position"
    GROSS_POSITION = "gross_position"
    LOSS = "loss"
    ORDER_COUNT = "order_count"


class LimitAction(str, Enum):
    WARN = "warn"
    REQUIRE_APPROVAL = "require_approval"
    BLOCK = "block"


@dataclass(frozen=True)
class ComplianceLimit:
    limit_id: str
    metric: LimitMetric
    threshold: Decimal
    action: LimitAction
    account_id: Optional[str] = None
    strategy_id: Optional[str] = None
    symbol: Optional[str] = None
    counterparty: Optional[str] = None
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.limit_id.strip():
            raise ValidationError("compliance limit id cannot be blank")
        object.__setattr__(self, "metric", self.metric if isinstance(self.metric, LimitMetric) else LimitMetric(self.metric))
        object.__setattr__(self, "action", self.action if isinstance(self.action, LimitAction) else LimitAction(self.action))
        object.__setattr__(self, "threshold", as_decimal(self.threshold, "limit threshold", positive=True))
        if self.symbol:
            object.__setattr__(self, "symbol", normalize_symbol(self.symbol))

    def applies(self, context: Mapping[str, object]) -> bool:
        selectors = {
            "account_id": self.account_id,
            "strategy_id": self.strategy_id,
            "symbol": self.symbol,
            "counterparty": self.counterparty,
        }
        return self.enabled and all(expected is None or context.get(name) == expected for name, expected in selectors.items())


@dataclass(frozen=True)
class LimitBreach:
    limit_id: str
    metric: LimitMetric
    observed: Decimal
    threshold: Decimal
    action: LimitAction
    excess: Decimal


@dataclass(frozen=True)
class LimitResult:
    breaches: Tuple[LimitBreach, ...]

    @property
    def blocked(self) -> bool:
        return any(item.action is LimitAction.BLOCK for item in self.breaches)

    @property
    def approval_required(self) -> bool:
        return any(item.action is LimitAction.REQUIRE_APPROVAL for item in self.breaches)

    @property
    def allowed(self) -> bool:
        return not self.blocked


class LimitEngine:
    def __init__(self) -> None:
        self._limits: Dict[str, ComplianceLimit] = {}

    def add(self, limit: ComplianceLimit) -> None:
        if limit.limit_id in self._limits:
            raise ValidationError("compliance limit id already exists")
        self._limits[limit.limit_id] = limit

    def evaluate(self, context: Mapping[str, object], metrics: Mapping[object, object]) -> LimitResult:
        parsed: Dict[LimitMetric, Decimal] = {}
        for key, value in metrics.items():
            metric = key if isinstance(key, LimitMetric) else LimitMetric(str(key))
            parsed[metric] = as_decimal(value, metric.value)
        breaches = []
        for limit in self._limits.values():
            if not limit.applies(context) or limit.metric not in parsed:
                continue
            observed = abs(parsed[limit.metric])
            if observed > limit.threshold:
                breaches.append(LimitBreach(limit.limit_id, limit.metric, observed, limit.threshold, limit.action, observed - limit.threshold))
        priority = {LimitAction.BLOCK: 0, LimitAction.REQUIRE_APPROVAL: 1, LimitAction.WARN: 2}
        breaches.sort(key=lambda item: (priority[item.action], item.limit_id))
        return LimitResult(tuple(breaches))

    def headroom(self, context: Mapping[str, object], metric: LimitMetric, current: object) -> Optional[Decimal]:
        value = abs(as_decimal(current, "current metric"))
        applicable = [item.threshold - value for item in self._limits.values() if item.metric is metric and item.applies(context)]
        return min(applicable) if applicable else None
