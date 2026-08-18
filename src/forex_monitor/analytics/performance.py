"""Return, volatility, drawdown, and risk-adjusted performance statistics."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Iterable, Optional, Sequence, Tuple

from forex_monitor.analytics.indicators import mean, standard_deviation
from forex_monitor.errors import AnalyticsError
from forex_monitor.models import as_decimal


ZERO = Decimal("0")
ONE = Decimal("1")


def simple_returns(values: Sequence[object]) -> Tuple[Decimal, ...]:
    prices = tuple(as_decimal(value, "price", positive=True) for value in values)
    if len(prices) < 2:
        raise AnalyticsError("returns require at least two prices")
    return tuple((current / previous) - ONE for previous, current in zip(prices, prices[1:]))


def log_returns(values: Sequence[object]) -> Tuple[Decimal, ...]:
    prices = tuple(as_decimal(value, "price", positive=True) for value in values)
    if len(prices) < 2:
        raise AnalyticsError("returns require at least two prices")
    result = []
    with localcontext() as context:
        context.prec = max(context.prec, 34)
        for previous, current in zip(prices, prices[1:]):
            ratio = current / previous
            try:
                result.append(ratio.ln())
            except AttributeError as error:
                raise AnalyticsError("log returns require Decimal.ln support") from error
    return tuple(result)


def cumulative_returns(returns: Sequence[object]) -> Tuple[Decimal, ...]:
    values = tuple(as_decimal(value, "return") for value in returns)
    wealth = ONE
    result = []
    for value in values:
        if value < -ONE:
            raise AnalyticsError("a simple return cannot be below -100%")
        wealth *= ONE + value
        result.append(wealth - ONE)
    return tuple(result)


def annualized_return(returns: Sequence[object], periods_per_year: int) -> Decimal:
    values = tuple(as_decimal(value, "return") for value in returns)
    if not values:
        raise AnalyticsError("annualized return requires observations")
    if periods_per_year < 1:
        raise AnalyticsError("periods per year must be positive")
    wealth = ONE
    for value in values:
        if value < -ONE:
            raise AnalyticsError("a simple return cannot be below -100%")
        wealth *= ONE + value
    if wealth == ZERO:
        return -ONE
    exponent = Decimal(periods_per_year) / Decimal(len(values))
    with localcontext() as context:
        context.prec = max(context.prec, 34)
        try:
            return (wealth.ln() * exponent).exp() - ONE
        except AttributeError as error:
            raise AnalyticsError("annualization requires Decimal exp/log support") from error


def annualized_volatility(returns: Sequence[object], periods_per_year: int) -> Decimal:
    values = tuple(as_decimal(value, "return") for value in returns)
    if len(values) < 2:
        return ZERO
    if periods_per_year < 1:
        raise AnalyticsError("periods per year must be positive")
    with localcontext() as context:
        context.prec = max(context.prec, 34)
        return standard_deviation(values, sample=True) * Decimal(periods_per_year).sqrt()


def downside_deviation(
    returns: Sequence[object],
    *,
    target: object = ZERO,
    periods_per_year: int = 1,
) -> Decimal:
    values = tuple(as_decimal(value, "return") for value in returns)
    if not values:
        raise AnalyticsError("downside deviation requires observations")
    threshold = as_decimal(target, "target return")
    downside = tuple(min(value - threshold, ZERO) for value in values)
    squared = sum((value * value for value in downside), ZERO) / Decimal(len(downside))
    with localcontext() as context:
        context.prec = max(context.prec, 34)
        return squared.sqrt() * Decimal(periods_per_year).sqrt()


@dataclass(frozen=True)
class DrawdownPoint:
    index: int
    wealth: Decimal
    peak: Decimal
    drawdown: Decimal


def drawdown_series(returns: Sequence[object]) -> Tuple[DrawdownPoint, ...]:
    values = tuple(as_decimal(value, "return") for value in returns)
    wealth = ONE
    peak = ONE
    result = []
    for index, value in enumerate(values):
        if value < -ONE:
            raise AnalyticsError("a simple return cannot be below -100%")
        wealth *= ONE + value
        peak = max(peak, wealth)
        drawdown = ZERO if peak == ZERO else wealth / peak - ONE
        result.append(DrawdownPoint(index, wealth, peak, drawdown))
    return tuple(result)


def maximum_drawdown(returns: Sequence[object]) -> Decimal:
    points = drawdown_series(returns)
    return min((point.drawdown for point in points), default=ZERO)


def sharpe_ratio(
    returns: Sequence[object],
    *,
    risk_free_rate: object = ZERO,
    periods_per_year: int = 252,
) -> Optional[Decimal]:
    values = tuple(as_decimal(value, "return") for value in returns)
    if len(values) < 2:
        return None
    annual_risk_free = as_decimal(risk_free_rate, "risk-free rate")
    periodic_risk_free = annual_risk_free / Decimal(periods_per_year)
    excess = tuple(value - periodic_risk_free for value in values)
    deviation = standard_deviation(excess, sample=True)
    if deviation == ZERO:
        return None
    with localcontext() as context:
        context.prec = max(context.prec, 34)
        return mean(excess) / deviation * Decimal(periods_per_year).sqrt()


def sortino_ratio(
    returns: Sequence[object],
    *,
    target_return: object = ZERO,
    periods_per_year: int = 252,
) -> Optional[Decimal]:
    values = tuple(as_decimal(value, "return") for value in returns)
    if not values:
        return None
    target = as_decimal(target_return, "target return")
    deviation = downside_deviation(
        values,
        target=target / Decimal(periods_per_year),
        periods_per_year=periods_per_year,
    )
    if deviation == ZERO:
        return None
    return (mean(values) * Decimal(periods_per_year) - target) / deviation


@dataclass(frozen=True)
class PerformanceSummary:
    observation_count: int
    total_return: Decimal
    annualized_return: Decimal
    annualized_volatility: Decimal
    maximum_drawdown: Decimal
    sharpe_ratio: Optional[Decimal]
    sortino_ratio: Optional[Decimal]
    winning_periods: int
    losing_periods: int
    flat_periods: int
    best_period: Decimal
    worst_period: Decimal
    average_period: Decimal

    @property
    def win_rate(self) -> Decimal:
        if self.observation_count == 0:
            return ZERO
        return Decimal(self.winning_periods) / Decimal(self.observation_count)

    def as_dict(self) -> dict[str, object]:
        return {
            "observationCount": self.observation_count,
            "totalReturn": str(self.total_return),
            "annualizedReturn": str(self.annualized_return),
            "annualizedVolatility": str(self.annualized_volatility),
            "maximumDrawdown": str(self.maximum_drawdown),
            "sharpeRatio": str(self.sharpe_ratio) if self.sharpe_ratio is not None else None,
            "sortinoRatio": str(self.sortino_ratio) if self.sortino_ratio is not None else None,
            "winningPeriods": self.winning_periods,
            "losingPeriods": self.losing_periods,
            "flatPeriods": self.flat_periods,
            "winRate": str(self.win_rate),
            "bestPeriod": str(self.best_period),
            "worstPeriod": str(self.worst_period),
            "averagePeriod": str(self.average_period),
        }


def summarize_returns(
    returns: Sequence[object],
    *,
    periods_per_year: int = 252,
    risk_free_rate: object = ZERO,
) -> PerformanceSummary:
    values = tuple(as_decimal(value, "return") for value in returns)
    if not values:
        raise AnalyticsError("performance summary requires returns")
    cumulative = cumulative_returns(values)
    return PerformanceSummary(
        observation_count=len(values),
        total_return=cumulative[-1],
        annualized_return=annualized_return(values, periods_per_year),
        annualized_volatility=annualized_volatility(values, periods_per_year),
        maximum_drawdown=maximum_drawdown(values),
        sharpe_ratio=sharpe_ratio(
            values,
            risk_free_rate=risk_free_rate,
            periods_per_year=periods_per_year,
        ),
        sortino_ratio=sortino_ratio(values, periods_per_year=periods_per_year),
        winning_periods=sum(value > ZERO for value in values),
        losing_periods=sum(value < ZERO for value in values),
        flat_periods=sum(value == ZERO for value in values),
        best_period=max(values),
        worst_period=min(values),
        average_period=mean(values),
    )

