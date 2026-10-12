"""Token-bucket quotas with client and route partitions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from threading import RLock
from typing import Dict, Optional, Tuple

from forex_monitor.api.models import text, utc
from forex_monitor.errors import ValidationError


@dataclass(frozen=True)
class RateLimitPolicy:
    policy_id: str
    capacity: Decimal
    refill_per_second: Decimal
    cost: Decimal = Decimal("1")

    def __post_init__(self) -> None:
        object.__setattr__(self, "policy_id", text(self.policy_id, "rate-limit policy id"))
        capacity = Decimal(str(self.capacity))
        refill = Decimal(str(self.refill_per_second))
        cost = Decimal(str(self.cost))
        if capacity <= 0 or refill <= 0 or cost <= 0 or cost > capacity:
            raise ValidationError("rate-limit policy values are invalid")
        object.__setattr__(self, "capacity", capacity)
        object.__setattr__(self, "refill_per_second", refill)
        object.__setattr__(self, "cost", cost)


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    limit: Decimal
    remaining: Decimal
    retry_after_seconds: Decimal
    reset_after_seconds: Decimal


@dataclass
class _Bucket:
    tokens: Decimal
    updated_at: datetime


class RateLimiter:
    def __init__(self) -> None:
        self._policies: Dict[str, RateLimitPolicy] = {}
        self._routes: Dict[str, str] = {}
        self._buckets: Dict[Tuple[str, str], _Bucket] = {}
        self._lock = RLock()

    def add_policy(self, policy: RateLimitPolicy) -> None:
        with self._lock:
            self._policies[policy.policy_id] = policy

    def bind(self, route: str, policy_id: str) -> None:
        with self._lock:
            if policy_id not in self._policies:
                raise ValidationError("rate-limit policy does not exist")
            self._routes[text(route, "route")] = policy_id

    def check(self, client_id: str, route: str, *, at: datetime, cost: Optional[object] = None, consume: bool = True) -> RateLimitDecision:
        instant = utc(at, "rate-limit time")
        with self._lock:
            policy_id = self._routes.get(route)
            if policy_id is None:
                return RateLimitDecision(True, Decimal("Infinity"), Decimal("Infinity"), Decimal("0"), Decimal("0"))
            policy = self._policies[policy_id]
            requested = Decimal(str(cost)) if cost is not None else policy.cost
            if requested <= 0 or requested > policy.capacity:
                raise ValidationError("rate-limit cost is invalid")
            key = (client_id, policy_id)
            bucket = self._buckets.get(key, _Bucket(policy.capacity, instant))
            elapsed = max(Decimal("0"), Decimal(str((instant - bucket.updated_at).total_seconds())))
            available = min(policy.capacity, bucket.tokens + elapsed * policy.refill_per_second)
            allowed = available >= requested
            remaining = available - requested if allowed and consume else available
            if consume:
                self._buckets[key] = _Bucket(remaining, instant)
            retry = (requested - available) / policy.refill_per_second if not allowed else Decimal("0")
            reset = (policy.capacity - remaining) / policy.refill_per_second
            return RateLimitDecision(allowed, policy.capacity, remaining, max(Decimal("0"), retry), max(Decimal("0"), reset))

    def reset(self, client_id: str, policy_id: Optional[str] = None) -> int:
        with self._lock:
            keys = [key for key in self._buckets if key[0] == client_id and (policy_id is None or key[1] == policy_id)]
            for key in keys:
                self._buckets.pop(key)
            return len(keys)
