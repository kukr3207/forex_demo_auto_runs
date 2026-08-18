"""Dependency-free technical indicators using Decimal arithmetic."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Iterable, Optional, Sequence, Tuple

from forex_monitor.errors import AnalyticsError
from forex_monitor.models import Candle, as_decimal


ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")


def _values(values: Iterable[object], name: str = "values") -> Tuple[Decimal, ...]:
    result = tuple(as_decimal(value, name) for value in values)
    if not result:
        raise AnalyticsError(f"{name} cannot be empty")
    return result


def _period(period: int, length: int, *, minimum: int = 1) -> int:
    if isinstance(period, bool) or not isinstance(period, int):
        raise AnalyticsError("period must be an integer")
    if period < minimum:
        raise AnalyticsError(f"period must be at least {minimum}")
    if period > length:
        raise AnalyticsError("period exceeds available observations")
    return period


def mean(values: Iterable[object]) -> Decimal:
    numbers = _values(values)
    return sum(numbers, ZERO) / Decimal(len(numbers))


def variance(values: Iterable[object], *, sample: bool = False) -> Decimal:
    numbers = _values(values)
    if sample and len(numbers) < 2:
        raise AnalyticsError("sample variance requires at least two observations")
    center = sum(numbers, ZERO) / Decimal(len(numbers))
    divisor = len(numbers) - 1 if sample else len(numbers)
    return sum(((value - center) ** 2 for value in numbers), ZERO) / Decimal(divisor)


def standard_deviation(values: Iterable[object], *, sample: bool = False) -> Decimal:
    value = variance(values, sample=sample)
    with localcontext() as context:
        context.prec = max(context.prec, 34)
        return value.sqrt()


def simple_moving_average(values: Sequence[object], period: int) -> Tuple[Optional[Decimal], ...]:
    numbers = _values(values)
    _period(period, len(numbers))
    result: list[Optional[Decimal]] = [None] * (period - 1)
    running = sum(numbers[:period], ZERO)
    result.append(running / Decimal(period))
    for index in range(period, len(numbers)):
        running += numbers[index] - numbers[index - period]
        result.append(running / Decimal(period))
    return tuple(result)


def weighted_moving_average(values: Sequence[object], period: int) -> Tuple[Optional[Decimal], ...]:
    numbers = _values(values)
    _period(period, len(numbers))
    weights = tuple(Decimal(index) for index in range(1, period + 1))
    divisor = sum(weights, ZERO)
    result: list[Optional[Decimal]] = [None] * (period - 1)
    for end in range(period, len(numbers) + 1):
        window = numbers[end - period : end]
        result.append(sum((value * weight for value, weight in zip(window, weights)), ZERO) / divisor)
    return tuple(result)


def exponential_moving_average(
    values: Sequence[object],
    period: int,
    *,
    seed: str = "sma",
) -> Tuple[Optional[Decimal], ...]:
    numbers = _values(values)
    _period(period, len(numbers))
    if seed not in {"sma", "first"}:
        raise AnalyticsError("EMA seed must be sma or first")
    multiplier = Decimal("2") / Decimal(period + 1)
    if seed == "first":
        result: list[Optional[Decimal]] = [numbers[0]]
        previous = numbers[0]
        start = 1
    else:
        result = [None] * (period - 1)
        previous = sum(numbers[:period], ZERO) / Decimal(period)
        result.append(previous)
        start = period
    for value in numbers[start:]:
        previous = (value - previous) * multiplier + previous
        result.append(previous)
    return tuple(result)


def rolling_min(values: Sequence[object], period: int) -> Tuple[Optional[Decimal], ...]:
    numbers = _values(values)
    _period(period, len(numbers))
    result: list[Optional[Decimal]] = [None] * (period - 1)
    result.extend(min(numbers[end - period : end]) for end in range(period, len(numbers) + 1))
    return tuple(result)


def rolling_max(values: Sequence[object], period: int) -> Tuple[Optional[Decimal], ...]:
    numbers = _values(values)
    _period(period, len(numbers))
    result: list[Optional[Decimal]] = [None] * (period - 1)
    result.extend(max(numbers[end - period : end]) for end in range(period, len(numbers) + 1))
    return tuple(result)


def rolling_standard_deviation(
    values: Sequence[object],
    period: int,
) -> Tuple[Optional[Decimal], ...]:
    numbers = _values(values)
    _period(period, len(numbers), minimum=2)
    result: list[Optional[Decimal]] = [None] * (period - 1)
    for end in range(period, len(numbers) + 1):
        result.append(standard_deviation(numbers[end - period : end]))
    return tuple(result)


def momentum(values: Sequence[object], period: int) -> Tuple[Optional[Decimal], ...]:
    numbers = _values(values)
    _period(period, len(numbers) - 1)
    result: list[Optional[Decimal]] = [None] * period
    result.extend(numbers[index] - numbers[index - period] for index in range(period, len(numbers)))
    return tuple(result)


def rate_of_change(values: Sequence[object], period: int) -> Tuple[Optional[Decimal], ...]:
    numbers = _values(values)
    _period(period, len(numbers) - 1)
    result: list[Optional[Decimal]] = [None] * period
    for index in range(period, len(numbers)):
        previous = numbers[index - period]
        result.append(None if previous == ZERO else (numbers[index] - previous) / previous * HUNDRED)
    return tuple(result)


def relative_strength_index(
    values: Sequence[object],
    period: int = 14,
) -> Tuple[Optional[Decimal], ...]:
    numbers = _values(values)
    _period(period, len(numbers) - 1)
    changes = tuple(numbers[index] - numbers[index - 1] for index in range(1, len(numbers)))
    gains = tuple(max(change, ZERO) for change in changes)
    losses = tuple(max(-change, ZERO) for change in changes)
    average_gain = sum(gains[:period], ZERO) / Decimal(period)
    average_loss = sum(losses[:period], ZERO) / Decimal(period)
    result: list[Optional[Decimal]] = [None] * period
    result.append(_rsi_value(average_gain, average_loss))
    for index in range(period, len(changes)):
        average_gain = (average_gain * Decimal(period - 1) + gains[index]) / Decimal(period)
        average_loss = (average_loss * Decimal(period - 1) + losses[index]) / Decimal(period)
        result.append(_rsi_value(average_gain, average_loss))
    return tuple(result)


def _rsi_value(average_gain: Decimal, average_loss: Decimal) -> Decimal:
    if average_loss == ZERO:
        return HUNDRED if average_gain > ZERO else Decimal("50")
    relative_strength = average_gain / average_loss
    return HUNDRED - HUNDRED / (ONE + relative_strength)


def true_range(candles: Sequence[Candle]) -> Tuple[Decimal, ...]:
    if not candles:
        raise AnalyticsError("candles cannot be empty")
    result = [candles[0].high - candles[0].low]
    for previous, current in zip(candles, candles[1:]):
        result.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    return tuple(result)


def average_true_range(
    candles: Sequence[Candle],
    period: int = 14,
) -> Tuple[Optional[Decimal], ...]:
    ranges = true_range(candles)
    _period(period, len(ranges))
    first = sum(ranges[:period], ZERO) / Decimal(period)
    result: list[Optional[Decimal]] = [None] * (period - 1) + [first]
    previous = first
    for value in ranges[period:]:
        previous = (previous * Decimal(period - 1) + value) / Decimal(period)
        result.append(previous)
    return tuple(result)


@dataclass(frozen=True)
class BollingerPoint:
    middle: Decimal
    upper: Decimal
    lower: Decimal
    width: Decimal
    percent_b: Optional[Decimal]


def bollinger_bands(
    values: Sequence[object],
    period: int = 20,
    deviations: object = Decimal("2"),
) -> Tuple[Optional[BollingerPoint], ...]:
    numbers = _values(values)
    _period(period, len(numbers), minimum=2)
    factor = as_decimal(deviations, "Bollinger deviation multiplier", positive=True)
    result: list[Optional[BollingerPoint]] = [None] * (period - 1)
    for end in range(period, len(numbers) + 1):
        window = numbers[end - period : end]
        middle = mean(window)
        distance = standard_deviation(window) * factor
        upper = middle + distance
        lower = middle - distance
        width = ZERO if middle == ZERO else (upper - lower) / middle
        percent_b = None if upper == lower else (window[-1] - lower) / (upper - lower)
        result.append(BollingerPoint(middle, upper, lower, width, percent_b))
    return tuple(result)


@dataclass(frozen=True)
class MacdPoint:
    line: Decimal
    signal: Optional[Decimal]
    histogram: Optional[Decimal]


def macd(
    values: Sequence[object],
    *,
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> Tuple[Optional[MacdPoint], ...]:
    numbers = _values(values)
    if fast_period >= slow_period:
        raise AnalyticsError("MACD fast period must be less than slow period")
    _period(slow_period, len(numbers), minimum=2)
    if signal_period < 1:
        raise AnalyticsError("MACD signal period must be positive")
    fast = exponential_moving_average(numbers, fast_period)
    slow = exponential_moving_average(numbers, slow_period)
    lines: list[Optional[Decimal]] = []
    defined_lines = []
    for fast_value, slow_value in zip(fast, slow):
        if fast_value is None or slow_value is None:
            lines.append(None)
        else:
            line = fast_value - slow_value
            lines.append(line)
            defined_lines.append(line)
    signal_values: Tuple[Optional[Decimal], ...]
    if len(defined_lines) < signal_period:
        signal_values = tuple([None] * len(defined_lines))
    else:
        signal_values = exponential_moving_average(defined_lines, signal_period)
    result: list[Optional[MacdPoint]] = []
    signal_index = 0
    for line in lines:
        if line is None:
            result.append(None)
            continue
        signal_value = signal_values[signal_index]
        signal_index += 1
        result.append(
            MacdPoint(
                line=line,
                signal=signal_value,
                histogram=line - signal_value if signal_value is not None else None,
            )
        )
    return tuple(result)


@dataclass(frozen=True)
class StochasticPoint:
    percent_k: Decimal
    percent_d: Optional[Decimal]


def stochastic_oscillator(
    candles: Sequence[Candle],
    period: int = 14,
    smooth: int = 3,
) -> Tuple[Optional[StochasticPoint], ...]:
    if not candles:
        raise AnalyticsError("candles cannot be empty")
    _period(period, len(candles))
    if smooth < 1:
        raise AnalyticsError("stochastic smoothing must be positive")
    raw_k: list[Decimal] = []
    for end in range(period, len(candles) + 1):
        window = candles[end - period : end]
        high = max(candle.high for candle in window)
        low = min(candle.low for candle in window)
        raw_k.append(Decimal("50") if high == low else (window[-1].close - low) / (high - low) * HUNDRED)
    smoothed = (
        simple_moving_average(raw_k, smooth)
        if len(raw_k) >= smooth
        else tuple([None] * len(raw_k))
    )
    result: list[Optional[StochasticPoint]] = [None] * (period - 1)
    result.extend(StochasticPoint(k, d) for k, d in zip(raw_k, smoothed))
    return tuple(result)


def typical_price(candles: Sequence[Candle]) -> Tuple[Decimal, ...]:
    if not candles:
        raise AnalyticsError("candles cannot be empty")
    return tuple((candle.high + candle.low + candle.close) / Decimal("3") for candle in candles)


def volume_weighted_average_price(candles: Sequence[Candle]) -> Tuple[Optional[Decimal], ...]:
    if not candles:
        raise AnalyticsError("candles cannot be empty")
    cumulative_value = ZERO
    cumulative_volume = ZERO
    result = []
    for price, candle in zip(typical_price(candles), candles):
        cumulative_value += price * candle.volume
        cumulative_volume += candle.volume
        result.append(None if cumulative_volume == ZERO else cumulative_value / cumulative_volume)
    return tuple(result)


def z_scores(values: Sequence[object], period: int) -> Tuple[Optional[Decimal], ...]:
    numbers = _values(values)
    _period(period, len(numbers), minimum=2)
    result: list[Optional[Decimal]] = [None] * (period - 1)
    for end in range(period, len(numbers) + 1):
        window = numbers[end - period : end]
        center = mean(window)
        deviation = standard_deviation(window)
        result.append(ZERO if deviation == ZERO else (window[-1] - center) / deviation)
    return tuple(result)


def percentile(values: Sequence[object], quantile: object) -> Decimal:
    numbers = sorted(_values(values))
    q = as_decimal(quantile, "quantile")
    if q < ZERO or q > ONE:
        raise AnalyticsError("quantile must be between 0 and 1")
    if len(numbers) == 1:
        return numbers[0]
    position = q * Decimal(len(numbers) - 1)
    lower_index = int(position)
    upper_index = min(lower_index + 1, len(numbers) - 1)
    fraction = position - Decimal(lower_index)
    return numbers[lower_index] + (numbers[upper_index] - numbers[lower_index]) * fraction


def linear_slope(values: Sequence[object]) -> Decimal:
    numbers = _values(values)
    if len(numbers) < 2:
        raise AnalyticsError("linear slope requires at least two observations")
    xs = tuple(Decimal(index) for index in range(len(numbers)))
    mean_x = mean(xs)
    mean_y = mean(numbers)
    numerator = sum(((x - mean_x) * (y - mean_y) for x, y in zip(xs, numbers)), ZERO)
    denominator = sum(((x - mean_x) ** 2 for x in xs), ZERO)
    return ZERO if denominator == ZERO else numerator / denominator

