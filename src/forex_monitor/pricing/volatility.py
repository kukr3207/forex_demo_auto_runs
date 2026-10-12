"""Delta-based volatility smiles and expiry surfaces."""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from math import exp, log
from typing import Dict, Iterable, Mapping, Sequence, Tuple

from forex_monitor.errors import ValidationError
from forex_monitor.models import as_decimal, normalize_symbol
from forex_monitor.pricing.day_count import DayCount, year_fraction


@dataclass(frozen=True, order=True)
class SmilePoint:
    delta: Decimal
    volatility: Decimal

    def __post_init__(self) -> None:
        delta = as_decimal(self.delta, "delta", positive=True)
        volatility = as_decimal(self.volatility, "volatility", positive=True)
        if delta >= 1:
            raise ValidationError("smile delta must be below one")
        if volatility > Decimal("5"):
            raise ValidationError("volatility must be expressed as a decimal")
        object.__setattr__(self, "delta", delta)
        object.__setattr__(self, "volatility", volatility)


class VolatilitySmile:
    def __init__(
        self,
        symbol: str,
        expiry: date,
        points: Iterable[SmilePoint],
    ) -> None:
        ordered = tuple(sorted(points, key=lambda item: item.delta))
        if len(ordered) < 2:
            raise ValidationError("volatility smile requires at least two points")
        if len({point.delta for point in ordered}) != len(ordered):
            raise ValidationError("volatility smile deltas must be unique")
        self.symbol = normalize_symbol(symbol)
        self.expiry = expiry
        self.points = ordered
        self._deltas = tuple(point.delta for point in ordered)

    def volatility(self, delta: object) -> Decimal:
        target = as_decimal(delta, "delta", positive=True)
        if target >= 1:
            raise ValidationError("delta must be below one")
        position = bisect_left(self._deltas, target)
        if position == 0:
            return self.points[0].volatility
        if position == len(self.points):
            return self.points[-1].volatility
        if self.points[position].delta == target:
            return self.points[position].volatility
        left = self.points[position - 1]
        right = self.points[position]
        weight = (target - left.delta) / (right.delta - left.delta)
        return left.volatility + (right.volatility - left.volatility) * weight

    @property
    def atm(self) -> Decimal:
        return self.volatility(Decimal("0.5"))

    def risk_reversal(self, delta: object = Decimal("0.25")) -> Decimal:
        parsed = as_decimal(delta, "delta", positive=True)
        return self.volatility(Decimal("1") - parsed) - self.volatility(parsed)

    def butterfly(self, delta: object = Decimal("0.25")) -> Decimal:
        parsed = as_decimal(delta, "delta", positive=True)
        wings = (self.volatility(parsed) + self.volatility(Decimal("1") - parsed)) / Decimal("2")
        return wings - self.atm

    def bump(self, absolute_change: object) -> "VolatilitySmile":
        change = as_decimal(absolute_change, "volatility change")
        points = []
        for point in self.points:
            value = point.volatility + change
            if value <= 0:
                raise ValidationError("volatility bump creates a non-positive value")
            points.append(SmilePoint(point.delta, value))
        return VolatilitySmile(self.symbol, self.expiry, points)


class VolatilitySurface:
    def __init__(
        self,
        symbol: str,
        valuation_date: date,
        smiles: Iterable[VolatilitySmile],
        *,
        day_count: DayCount = DayCount.ACT_365_FIXED,
    ) -> None:
        normalized = normalize_symbol(symbol)
        ordered = tuple(sorted(smiles, key=lambda item: item.expiry))
        if not ordered:
            raise ValidationError("volatility surface requires at least one smile")
        if any(smile.symbol != normalized for smile in ordered):
            raise ValidationError("all smiles must use the surface symbol")
        if any(smile.expiry <= valuation_date for smile in ordered):
            raise ValidationError("smile expiries must follow valuation date")
        if len({smile.expiry for smile in ordered}) != len(ordered):
            raise ValidationError("smile expiries must be unique")
        self.symbol = normalized
        self.valuation_date = valuation_date
        self.smiles = ordered
        self.day_count = DayCount.parse(day_count)
        self._expiries = tuple(smile.expiry for smile in ordered)

    def volatility(self, expiry: date, delta: object) -> Decimal:
        if expiry <= self.valuation_date:
            raise ValidationError("option expiry must follow valuation date")
        position = bisect_left(self._expiries, expiry)
        if position == 0:
            return self.smiles[0].volatility(delta)
        if position == len(self.smiles):
            return self.smiles[-1].volatility(delta)
        if self.smiles[position].expiry == expiry:
            return self.smiles[position].volatility(delta)
        left = self.smiles[position - 1]
        right = self.smiles[position]
        target_time = year_fraction(self.valuation_date, expiry, self.day_count)
        left_time = year_fraction(self.valuation_date, left.expiry, self.day_count)
        right_time = year_fraction(self.valuation_date, right.expiry, self.day_count)
        left_var = left.volatility(delta) ** 2 * left_time
        right_var = right.volatility(delta) ** 2 * right_time
        weight = (target_time - left_time) / (right_time - left_time)
        variance = left_var + (right_var - left_var) * weight
        return Decimal(str((float(variance / target_time)) ** 0.5))

    def total_variance(self, expiry: date, delta: object) -> Decimal:
        volatility = self.volatility(expiry, delta)
        time = year_fraction(self.valuation_date, expiry, self.day_count)
        return volatility * volatility * time

    def calendar_arbitrage(self, deltas: Sequence[object]) -> Tuple[Tuple[date, Decimal], ...]:
        violations = []
        previous: Dict[Decimal, Decimal] = {}
        for smile in self.smiles:
            for raw_delta in deltas:
                delta = as_decimal(raw_delta, "delta", positive=True)
                variance = self.total_variance(smile.expiry, delta)
                prior = previous.get(delta)
                if prior is not None and variance < prior:
                    violations.append((smile.expiry, delta))
                previous[delta] = variance
        return tuple(violations)

    def bump(self, absolute_change: object) -> "VolatilitySurface":
        return VolatilitySurface(
            self.symbol,
            self.valuation_date,
            (smile.bump(absolute_change) for smile in self.smiles),
            day_count=self.day_count,
        )
