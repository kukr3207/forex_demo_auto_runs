"""Discount and FX-forward curves with deterministic interpolation."""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from math import exp, log
from typing import Iterable, Mapping, Sequence, Tuple

from forex_monitor.errors import ValidationError
from forex_monitor.models import as_decimal, normalize_symbol
from forex_monitor.pricing.day_count import DayCount, year_fraction


@dataclass(frozen=True, order=True)
class DiscountNode:
    maturity: date
    discount_factor: Decimal

    def __post_init__(self) -> None:
        factor = as_decimal(self.discount_factor, "discount factor", positive=True)
        object.__setattr__(self, "discount_factor", factor)


@dataclass(frozen=True)
class CurvePoint:
    maturity: date
    discount_factor: Decimal
    zero_rate: Decimal
    forward_rate: Decimal


class DiscountCurve:
    def __init__(
        self,
        currency: str,
        valuation_date: date,
        nodes: Iterable[DiscountNode],
        *,
        day_count: DayCount = DayCount.ACT_365_FIXED,
    ) -> None:
        normalized = currency.strip().upper()
        if len(normalized) != 3 or not normalized.isalpha():
            raise ValidationError("curve currency must contain three letters")
        ordered = tuple(sorted(nodes, key=lambda item: item.maturity))
        if not ordered:
            raise ValidationError("discount curve requires at least one node")
        if any(node.maturity <= valuation_date for node in ordered):
            raise ValidationError("curve nodes must mature after valuation date")
        if len({node.maturity for node in ordered}) != len(ordered):
            raise ValidationError("curve node maturities must be unique")
        previous = Decimal("1.000000000001")
        for node in ordered:
            if node.discount_factor > previous:
                raise ValidationError("discount factors must be non-increasing")
            previous = node.discount_factor
        self.currency = normalized
        self.valuation_date = valuation_date
        self.nodes = ordered
        self.day_count = DayCount.parse(day_count)
        self._dates = tuple(node.maturity for node in ordered)

    def discount(self, maturity: date) -> Decimal:
        if maturity < self.valuation_date:
            raise ValidationError("cannot discount before curve valuation date")
        if maturity == self.valuation_date:
            return Decimal("1")
        position = bisect_left(self._dates, maturity)
        if position < len(self.nodes) and self.nodes[position].maturity == maturity:
            return self.nodes[position].discount_factor
        if position == 0:
            right = self.nodes[0]
            total = year_fraction(self.valuation_date, right.maturity, self.day_count)
            elapsed = year_fraction(self.valuation_date, maturity, self.day_count)
            return Decimal(str(exp(log(float(right.discount_factor)) * float(elapsed / total))))
        if position == len(self.nodes):
            left = self.nodes[-1]
            left_time = year_fraction(self.valuation_date, left.maturity, self.day_count)
            target_time = year_fraction(self.valuation_date, maturity, self.day_count)
            zero = -Decimal(str(log(float(left.discount_factor)))) / left_time
            return Decimal(str(exp(-float(zero * target_time))))
        left = self.nodes[position - 1]
        right = self.nodes[position]
        span = year_fraction(left.maturity, right.maturity, self.day_count)
        elapsed = year_fraction(left.maturity, maturity, self.day_count)
        weight = elapsed / span
        left_log = Decimal(str(log(float(left.discount_factor))))
        right_log = Decimal(str(log(float(right.discount_factor))))
        return Decimal(str(exp(float(left_log + (right_log - left_log) * weight))))

    def zero_rate(self, maturity: date) -> Decimal:
        fraction = year_fraction(self.valuation_date, maturity, self.day_count)
        if fraction == 0:
            return Decimal("0")
        return -Decimal(str(log(float(self.discount(maturity))))) / fraction

    def forward_rate(self, start: date, end: date) -> Decimal:
        if end <= start:
            raise ValidationError("forward-rate end must follow start")
        fraction = year_fraction(start, end, self.day_count)
        return (self.discount(start) / self.discount(end) - Decimal("1")) / fraction

    def points(self) -> Tuple[CurvePoint, ...]:
        result = []
        previous = self.valuation_date
        for node in self.nodes:
            result.append(
                CurvePoint(
                    node.maturity,
                    node.discount_factor,
                    self.zero_rate(node.maturity),
                    self.forward_rate(previous, node.maturity),
                )
            )
            previous = node.maturity
        return tuple(result)

    def bump(self, basis_points: object) -> "DiscountCurve":
        bump = as_decimal(basis_points, "basis-point bump") / Decimal("10000")
        nodes = []
        for node in self.nodes:
            maturity_time = year_fraction(self.valuation_date, node.maturity, self.day_count)
            bumped = Decimal(str(exp(-float((self.zero_rate(node.maturity) + bump) * maturity_time))))
            nodes.append(DiscountNode(node.maturity, bumped))
        return DiscountCurve(self.currency, self.valuation_date, nodes, day_count=self.day_count)

    @classmethod
    def from_zero_rates(
        cls,
        currency: str,
        valuation_date: date,
        rates: Mapping[date, object],
        *,
        day_count: DayCount = DayCount.ACT_365_FIXED,
    ) -> "DiscountCurve":
        nodes = []
        for maturity, value in rates.items():
            rate = as_decimal(value, "zero rate")
            time = year_fraction(valuation_date, maturity, day_count)
            nodes.append(DiscountNode(maturity, Decimal(str(exp(-float(rate * time))))))
        return cls(currency, valuation_date, nodes, day_count=day_count)


class FxForwardCurve:
    def __init__(
        self,
        symbol: str,
        spot: object,
        domestic: DiscountCurve,
        foreign: DiscountCurve,
    ) -> None:
        normalized = normalize_symbol(symbol)
        if len(normalized) != 6:
            raise ValidationError("forward curve requires a six-letter currency pair")
        if normalized[:3] != foreign.currency or normalized[3:] != domestic.currency:
            raise ValidationError("discount curves do not match the pair quotation")
        if domestic.valuation_date != foreign.valuation_date:
            raise ValidationError("discount curves must share a valuation date")
        self.symbol = normalized
        self.spot = as_decimal(spot, "spot", positive=True)
        self.domestic = domestic
        self.foreign = foreign

    @property
    def valuation_date(self) -> date:
        return self.domestic.valuation_date

    def forward(self, maturity: date) -> Decimal:
        return self.spot * self.foreign.discount(maturity) / self.domestic.discount(maturity)

    def points(self, maturity: date, pip_size: object = Decimal("0.0001")) -> Decimal:
        pip = as_decimal(pip_size, "pip size", positive=True)
        return (self.forward(maturity) - self.spot) / pip

    def outright_quotes(self, maturities: Sequence[date]) -> Tuple[Tuple[date, Decimal], ...]:
        if tuple(maturities) != tuple(sorted(maturities)):
            raise ValidationError("maturities must be sorted")
        return tuple((maturity, self.forward(maturity)) for maturity in maturities)
