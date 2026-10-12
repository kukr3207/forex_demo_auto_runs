"""FX-swap representation, pricing, and roll analysis."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Mapping, Tuple

from forex_monitor.errors import ValidationError
from forex_monitor.models import as_decimal, normalize_symbol
from forex_monitor.pricing.curves import FxForwardCurve
from forex_monitor.pricing.forwards import ForwardContract, ForwardSide, price_forward


@dataclass(frozen=True)
class FxSwap:
    swap_id: str
    symbol: str
    side: ForwardSide
    notional: Decimal
    near_rate: Decimal
    far_rate: Decimal
    near_date: date
    far_date: date
    trade_date: date

    def __post_init__(self) -> None:
        swap_id = self.swap_id.strip()
        if not swap_id:
            raise ValidationError("swap id cannot be blank")
        if not self.trade_date <= self.near_date < self.far_date:
            raise ValidationError("swap dates must be ordered from trade to near to far")
        object.__setattr__(self, "swap_id", swap_id)
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(self, "side", self.side if isinstance(self.side, ForwardSide) else ForwardSide(self.side))
        object.__setattr__(self, "notional", as_decimal(self.notional, "notional", positive=True))
        object.__setattr__(self, "near_rate", as_decimal(self.near_rate, "near rate", positive=True))
        object.__setattr__(self, "far_rate", as_decimal(self.far_rate, "far rate", positive=True))

    @property
    def swap_points(self) -> Decimal:
        return self.far_rate - self.near_rate

    def legs(self) -> Tuple[ForwardContract, ForwardContract]:
        opposite = (
            ForwardSide.SELL_BASE
            if self.side is ForwardSide.BUY_BASE
            else ForwardSide.BUY_BASE
        )
        near = ForwardContract(
            self.swap_id + ":near",
            self.symbol,
            self.side,
            self.notional,
            self.near_rate,
            self.near_date,
            self.trade_date,
        )
        far = ForwardContract(
            self.swap_id + ":far",
            self.symbol,
            opposite,
            self.notional,
            self.far_rate,
            self.far_date,
            self.trade_date,
        )
        return near, far


@dataclass(frozen=True)
class SwapQuote:
    swap_id: str
    near_fair_rate: Decimal
    far_fair_rate: Decimal
    fair_points: Decimal
    contracted_points: Decimal
    near_present_value: Decimal
    far_present_value: Decimal
    present_value: Decimal
    currency: str

    def as_dict(self) -> Mapping[str, object]:
        return {
            "swapId": self.swap_id,
            "nearFairRate": str(self.near_fair_rate),
            "farFairRate": str(self.far_fair_rate),
            "fairPoints": str(self.fair_points),
            "contractedPoints": str(self.contracted_points),
            "nearPresentValue": str(self.near_present_value),
            "farPresentValue": str(self.far_present_value),
            "presentValue": str(self.present_value),
            "currency": self.currency,
        }


def price_swap(swap: FxSwap, curve: FxForwardCurve) -> SwapQuote:
    if swap.symbol != curve.symbol:
        raise ValidationError("swap and curve symbols differ")
    near, far = swap.legs()
    near_quote = price_forward(near, curve)
    far_quote = price_forward(far, curve)
    near_fair = curve.forward(swap.near_date)
    far_fair = curve.forward(swap.far_date)
    return SwapQuote(
        swap.swap_id,
        near_fair,
        far_fair,
        far_fair - near_fair,
        swap.swap_points,
        near_quote.present_value,
        far_quote.present_value,
        near_quote.present_value + far_quote.present_value,
        swap.symbol[3:],
    )


def roll_swap(
    swap: FxSwap,
    new_far_date: date,
    curve: FxForwardCurve,
) -> FxSwap:
    if new_far_date <= swap.far_date:
        raise ValidationError("rolled far date must follow the existing far date")
    new_near_rate = curve.forward(swap.far_date)
    new_far_rate = curve.forward(new_far_date)
    return FxSwap(
        swap.swap_id + ":roll",
        swap.symbol,
        swap.side,
        swap.notional,
        new_near_rate,
        new_far_rate,
        swap.far_date,
        new_far_date,
        curve.valuation_date,
    )


def swap_cashflows(swap: FxSwap) -> Mapping[date, Mapping[str, Decimal]]:
    near, far = swap.legs()
    sign = swap.side.sign
    return {
        near.maturity: {
            swap.symbol[:3]: sign * swap.notional,
            swap.symbol[3:]: -sign * swap.notional * swap.near_rate,
        },
        far.maturity: {
            swap.symbol[:3]: -sign * swap.notional,
            swap.symbol[3:]: sign * swap.notional * swap.far_rate,
        },
    }
