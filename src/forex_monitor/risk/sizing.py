"""Position sizing from account risk and stop distance."""

from decimal import ROUND_DOWN, Decimal

from forex_monitor.models import as_decimal


def fixed_fractional_size(
    equity: object,
    risk_fraction: object,
    stop_distance: object,
    value_per_price_unit: object = Decimal("1"),
    lot_step: object = Decimal("0.01"),
) -> Decimal:
    capital = as_decimal(equity, "equity", positive=True)
    fraction = as_decimal(risk_fraction, "risk fraction", positive=True)
    distance = as_decimal(stop_distance, "stop distance", positive=True)
    unit_value = as_decimal(value_per_price_unit, "unit value", positive=True)
    step = as_decimal(lot_step, "lot step", positive=True)
    if fraction > Decimal("1"):
        raise ValueError("risk fraction cannot exceed one")
    raw = capital * fraction / (distance * unit_value)
    return (raw / step).to_integral_value(rounding=ROUND_DOWN) * step


def risk_amount(equity: object, risk_fraction: object) -> Decimal:
    capital = as_decimal(equity, "equity", positive=True)
    fraction = as_decimal(risk_fraction, "risk fraction", positive=True)
    if fraction > Decimal("1"):
        raise ValueError("risk fraction cannot exceed one")
    return capital * fraction
