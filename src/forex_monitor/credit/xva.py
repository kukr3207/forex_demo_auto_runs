"""Discrete CVA, DVA, and funding valuation adjustments."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Sequence, Tuple

from forex_monitor.errors import ValidationError
from forex_monitor.models import as_decimal


@dataclass(frozen=True)
class HazardPoint:
    year_fraction: Decimal
    survival_probability: Decimal

    def __post_init__(self) -> None:
        year = as_decimal(self.year_fraction, "hazard year fraction")
        survival = as_decimal(self.survival_probability, "survival probability")
        if year <= 0 or survival < 0 or survival > 1:
            raise ValidationError("hazard point is outside valid bounds")
        object.__setattr__(self, "year_fraction", year)
        object.__setattr__(self, "survival_probability", survival)


@dataclass(frozen=True)
class HazardCurve:
    points: Tuple[HazardPoint, ...]

    def __post_init__(self) -> None:
        points = tuple(self.points)
        if not points:
            raise ValidationError("hazard curve requires points")
        if any(right.year_fraction <= left.year_fraction for left, right in zip(points, points[1:])):
            raise ValidationError("hazard points must have increasing maturities")
        if any(right.survival_probability > left.survival_probability for left, right in zip(points, points[1:])):
            raise ValidationError("survival probabilities cannot increase")
        object.__setattr__(self, "points", points)


@dataclass(frozen=True)
class RecoveryAssumption:
    counterparty_recovery: Decimal
    own_recovery: Decimal

    def __post_init__(self) -> None:
        counterparty = as_decimal(self.counterparty_recovery, "counterparty recovery")
        own = as_decimal(self.own_recovery, "own recovery")
        if counterparty < 0 or counterparty > 1 or own < 0 or own > 1:
            raise ValidationError("recovery rates must be between zero and one")
        object.__setattr__(self, "counterparty_recovery", counterparty)
        object.__setattr__(self, "own_recovery", own)


@dataclass(frozen=True)
class ExposurePoint:
    year_fraction: Decimal
    expected_positive: Decimal
    expected_negative: Decimal
    discount_factor: Decimal
    funding_requirement: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        year = as_decimal(self.year_fraction, "exposure year fraction")
        positive = as_decimal(self.expected_positive, "expected positive exposure")
        negative = as_decimal(self.expected_negative, "expected negative exposure")
        discount = as_decimal(self.discount_factor, "discount factor")
        funding = as_decimal(self.funding_requirement, "funding requirement")
        if year <= 0 or positive < 0 or negative < 0 or discount <= 0 or funding < 0:
            raise ValidationError("exposure point is outside valid bounds")
        object.__setattr__(self, "year_fraction", year)
        object.__setattr__(self, "expected_positive", positive)
        object.__setattr__(self, "expected_negative", negative)
        object.__setattr__(self, "discount_factor", discount)
        object.__setattr__(self, "funding_requirement", funding)


@dataclass(frozen=True)
class XvaResult:
    cva: Decimal
    dva: Decimal
    fva: Decimal
    total_adjustment: Decimal
    expected_positive_exposure: Decimal
    expected_negative_exposure: Decimal


class XvaCalculator:
    @staticmethod
    def calculate(
        exposures: Sequence[ExposurePoint],
        counterparty_curve: HazardCurve,
        own_curve: HazardCurve,
        recovery: RecoveryAssumption,
        funding_spread: object,
    ) -> XvaResult:
        points = tuple(exposures)
        if not points:
            raise ValidationError("XVA calculation requires exposure points")
        if len(points) != len(counterparty_curve.points) or len(points) != len(own_curve.points):
            raise ValidationError("hazard and exposure curves must share a grid")
        spread = as_decimal(funding_spread, "funding spread")
        if spread < 0:
            raise ValidationError("funding spread cannot be negative")
        cva = Decimal("0")
        dva = Decimal("0")
        fva = Decimal("0")
        prior_counterparty_survival = Decimal("1")
        prior_own_survival = Decimal("1")
        for exposure, counterparty, own in zip(points, counterparty_curve.points, own_curve.points):
            if exposure.year_fraction != counterparty.year_fraction or exposure.year_fraction != own.year_fraction:
                raise ValidationError("hazard and exposure maturities differ")
            counterparty_default = prior_counterparty_survival - counterparty.survival_probability
            own_default = prior_own_survival - own.survival_probability
            cva += exposure.discount_factor * exposure.expected_positive * counterparty_default * (Decimal("1") - recovery.counterparty_recovery)
            dva += exposure.discount_factor * exposure.expected_negative * own_default * (Decimal("1") - recovery.own_recovery)
            fva += exposure.discount_factor * exposure.funding_requirement * spread * exposure.year_fraction
            prior_counterparty_survival = counterparty.survival_probability
            prior_own_survival = own.survival_probability
        expected_positive = sum((item.expected_positive for item in points), Decimal("0")) / Decimal(len(points))
        expected_negative = sum((item.expected_negative for item in points), Decimal("0")) / Decimal(len(points))
        return XvaResult(cva, dva, fva, -cva + dva - fva, expected_positive, expected_negative)

