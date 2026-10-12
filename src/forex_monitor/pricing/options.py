"""European FX options priced with the Garman-Kohlhagen model."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from math import erf, exp, log, pi, sqrt
from typing import Mapping, Optional

from forex_monitor.errors import ValidationError
from forex_monitor.models import as_decimal, normalize_symbol
from forex_monitor.pricing.curves import FxForwardCurve
from forex_monitor.pricing.day_count import DayCount, year_fraction


class OptionType(str, Enum):
    CALL = "call"
    PUT = "put"

    @property
    def sign(self) -> Decimal:
        return Decimal("1") if self is OptionType.CALL else Decimal("-1")


@dataclass(frozen=True)
class FxOption:
    option_id: str
    symbol: str
    option_type: OptionType
    notional: Decimal
    strike: Decimal
    expiry: date
    settlement_date: date
    premium: Decimal = Decimal("0")
    premium_currency: Optional[str] = None
    long: bool = True

    def __post_init__(self) -> None:
        identifier = self.option_id.strip()
        symbol = normalize_symbol(self.symbol)
        if not identifier:
            raise ValidationError("option id cannot be blank")
        if len(symbol) != 6:
            raise ValidationError("option symbol must be a currency pair")
        if self.settlement_date < self.expiry:
            raise ValidationError("option settlement cannot precede expiry")
        option_type = self.option_type if isinstance(self.option_type, OptionType) else OptionType(self.option_type)
        premium = as_decimal(self.premium, "premium")
        if premium < 0:
            raise ValidationError("option premium cannot be negative")
        premium_currency = self.premium_currency.strip().upper() if self.premium_currency else symbol[3:]
        if premium_currency not in {symbol[:3], symbol[3:]}:
            raise ValidationError("premium currency must belong to the pair")
        object.__setattr__(self, "option_id", identifier)
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "option_type", option_type)
        object.__setattr__(self, "notional", as_decimal(self.notional, "notional", positive=True))
        object.__setattr__(self, "strike", as_decimal(self.strike, "strike", positive=True))
        object.__setattr__(self, "premium", premium)
        object.__setattr__(self, "premium_currency", premium_currency)


@dataclass(frozen=True)
class OptionQuote:
    option_id: str
    value: Decimal
    unit_price: Decimal
    intrinsic_value: Decimal
    time_value: Decimal
    delta: Decimal
    gamma: Decimal
    vega: Decimal
    theta: Decimal
    volatility: Decimal
    forward: Decimal
    currency: str

    def as_dict(self) -> Mapping[str, object]:
        return {
            "optionId": self.option_id,
            "value": str(self.value),
            "unitPrice": str(self.unit_price),
            "intrinsicValue": str(self.intrinsic_value),
            "timeValue": str(self.time_value),
            "delta": str(self.delta),
            "gamma": str(self.gamma),
            "vega": str(self.vega),
            "theta": str(self.theta),
            "volatility": str(self.volatility),
            "forward": str(self.forward),
            "currency": self.currency,
        }


def normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + erf(value / sqrt(2.0)))


def normal_pdf(value: float) -> float:
    return exp(-0.5 * value * value) / sqrt(2.0 * pi)


def _d1_d2(spot: Decimal, strike: Decimal, domestic: Decimal, foreign: Decimal, vol: Decimal, time: Decimal) -> tuple[float, float]:
    sigma = float(vol)
    years = float(time)
    root_time = sqrt(years)
    numerator = log(float(spot / strike)) + (float(domestic - foreign) + 0.5 * sigma * sigma) * years
    d1 = numerator / (sigma * root_time)
    return d1, d1 - sigma * root_time


def price_option(
    option: FxOption,
    curve: FxForwardCurve,
    volatility: object,
    *,
    day_count: DayCount = DayCount.ACT_365_FIXED,
) -> OptionQuote:
    if option.symbol != curve.symbol:
        raise ValidationError("option and forward curve symbols differ")
    if option.expiry <= curve.valuation_date:
        raise ValidationError("cannot price an expired option")
    vol = as_decimal(volatility, "volatility", positive=True)
    if vol > Decimal("5"):
        raise ValidationError("volatility must be expressed as a decimal")
    time = year_fraction(curve.valuation_date, option.expiry, day_count)
    domestic_discount = curve.domestic.discount(option.expiry)
    foreign_discount = curve.foreign.discount(option.expiry)
    domestic_rate = -Decimal(str(log(float(domestic_discount)))) / time
    foreign_rate = -Decimal(str(log(float(foreign_discount)))) / time
    d1, d2 = _d1_d2(curve.spot, option.strike, domestic_rate, foreign_rate, vol, time)
    sign = float(option.option_type.sign)
    spot = float(curve.spot)
    strike = float(option.strike)
    dd = float(domestic_discount)
    fd = float(foreign_discount)
    unit = sign * (spot * fd * normal_cdf(sign * d1) - strike * dd * normal_cdf(sign * d2))
    position_sign = Decimal("1") if option.long else Decimal("-1")
    unit_decimal = Decimal(str(unit))
    intrinsic = max(option.option_type.sign * (curve.spot - option.strike), Decimal("0"))
    value = position_sign * option.notional * unit_decimal
    premium_cost = option.premium
    if option.premium_currency == option.symbol[:3]:
        premium_cost *= curve.spot
    value -= position_sign * premium_cost
    delta = Decimal(str(sign * fd * normal_cdf(sign * d1))) * position_sign * option.notional
    gamma = Decimal(str(fd * normal_pdf(d1) / (spot * float(vol) * sqrt(float(time))))) * option.notional
    vega = Decimal(str(spot * fd * normal_pdf(d1) * sqrt(float(time)))) * option.notional / Decimal("100")
    theta_unit = (
        -spot * fd * normal_pdf(d1) * float(vol) / (2 * sqrt(float(time)))
        + sign * float(foreign_rate) * spot * fd * normal_cdf(sign * d1)
        - sign * float(domestic_rate) * strike * dd * normal_cdf(sign * d2)
    ) / 365.0
    theta = Decimal(str(theta_unit)) * option.notional * position_sign
    forward = curve.forward(option.expiry)
    return OptionQuote(
        option.option_id,
        value,
        unit_decimal,
        intrinsic * option.notional,
        (unit_decimal - intrinsic) * option.notional,
        delta,
        gamma,
        vega,
        theta,
        vol,
        forward,
        option.symbol[3:],
    )


def implied_volatility(
    option: FxOption,
    curve: FxForwardCurve,
    target_unit_price: object,
    *,
    tolerance: Decimal = Decimal("0.00000001"),
    iterations: int = 100,
) -> Decimal:
    target = as_decimal(target_unit_price, "target option price")
    if target <= 0:
        raise ValidationError("target option price must be positive")
    low = Decimal("0.0001")
    high = Decimal("5")
    for _ in range(iterations):
        middle = (low + high) / Decimal("2")
        value = price_option(option, curve, middle).unit_price
        difference = value - target
        if abs(difference) <= tolerance:
            return middle
        if difference < 0:
            low = middle
        else:
            high = middle
    result = (low + high) / Decimal("2")
    if abs(price_option(option, curve, result).unit_price - target) > tolerance * Decimal("10"):
        raise ValidationError("target option price has no stable implied volatility")
    return result


def option_payoff(option: FxOption, fixing_rate: object) -> Decimal:
    rate = as_decimal(fixing_rate, "fixing rate", positive=True)
    intrinsic = max(option.option_type.sign * (rate - option.strike), Decimal("0"))
    sign = Decimal("1") if option.long else Decimal("-1")
    return sign * option.notional * intrinsic
