from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from forex_monitor.analytics.aggregation import (
    aggregate_quotes,
    ask_price,
    bid_price,
    resample_candles,
    validate_candle_continuity,
)
from forex_monitor.analytics.indicators import (
    average_true_range,
    bollinger_bands,
    exponential_moving_average,
    linear_slope,
    macd,
    mean,
    momentum,
    percentile,
    rate_of_change,
    relative_strength_index,
    rolling_max,
    rolling_min,
    rolling_standard_deviation,
    simple_moving_average,
    standard_deviation,
    stochastic_oscillator,
    true_range,
    variance,
    volume_weighted_average_price,
    weighted_moving_average,
    z_scores,
)
from forex_monitor.analytics.performance import (
    annualized_return,
    annualized_volatility,
    cumulative_returns,
    downside_deviation,
    drawdown_series,
    log_returns,
    maximum_drawdown,
    sharpe_ratio,
    simple_returns,
    sortino_ratio,
    summarize_returns,
)
from forex_monitor.analytics.service import AnalysisService, SignalPolicy
from forex_monitor.errors import AnalyticsError
from forex_monitor.models import Signal, Timeframe
from tests.support import BASE_TIME, candle, candle_series, quote_series


class BasicIndicatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.values = tuple(Decimal(index) for index in range(1, 11))

    def test_mean_variance_and_standard_deviation(self) -> None:
        self.assertEqual(mean((1, 2, 3, 4)), Decimal("2.5"))
        self.assertEqual(variance((1, 2, 3, 4)), Decimal("1.25"))
        self.assertEqual(variance((1, 2, 3, 4), sample=True), Decimal("1.666666666666666666666666667"))
        self.assertAlmostEqual(float(standard_deviation((1, 2, 3, 4))), 1.11803398875)

    def test_simple_moving_average(self) -> None:
        result = simple_moving_average(self.values, 3)
        self.assertEqual(result[:2], (None, None))
        self.assertEqual(result[2], Decimal("2"))
        self.assertEqual(result[-1], Decimal("9"))

    def test_weighted_moving_average(self) -> None:
        result = weighted_moving_average((1, 2, 3), 3)
        self.assertEqual(result[-1], Decimal("2.333333333333333333333333333"))

    def test_exponential_moving_average_seeds(self) -> None:
        sma_seed = exponential_moving_average(self.values, 3)
        first_seed = exponential_moving_average(self.values, 3, seed="first")
        self.assertEqual(sma_seed[2], Decimal("2"))
        self.assertEqual(first_seed[0], Decimal("1"))
        self.assertEqual(len(first_seed), len(self.values))

    def test_rolling_extremes(self) -> None:
        self.assertEqual(rolling_min((3, 2, 4, 1), 2), (None, Decimal("2"), Decimal("2"), Decimal("1")))
        self.assertEqual(rolling_max((3, 2, 4, 1), 2), (None, Decimal("3"), Decimal("4"), Decimal("4")))

    def test_rolling_deviation(self) -> None:
        result = rolling_standard_deviation((1, 2, 3, 4), 2)
        self.assertIsNone(result[0])
        self.assertEqual(result[1], Decimal("0.5"))

    def test_momentum_and_rate_of_change(self) -> None:
        self.assertEqual(momentum((10, 12, 15), 1), (None, Decimal("2"), Decimal("3")))
        self.assertEqual(rate_of_change((10, 12, 15), 1), (None, Decimal("20"), Decimal("25")))

    def test_period_validation(self) -> None:
        for function in (simple_moving_average, weighted_moving_average, exponential_moving_average):
            with self.subTest(function=function.__name__), self.assertRaises(AnalyticsError):
                function((1, 2), 3)


class OscillatorTests(unittest.TestCase):
    def test_rsi_for_rising_flat_and_falling_series(self) -> None:
        rising = relative_strength_index(tuple(range(1, 20)), 5)
        flat = relative_strength_index((1,) * 20, 5)
        falling = relative_strength_index(tuple(range(20, 0, -1)), 5)
        self.assertEqual(rising[-1], Decimal("100"))
        self.assertEqual(flat[-1], Decimal("50"))
        self.assertEqual(falling[-1], Decimal("0"))

    def test_bollinger_bands_center_and_width(self) -> None:
        bands = bollinger_bands((1, 2, 3, 4, 5), 5)[-1]
        assert bands is not None
        self.assertEqual(bands.middle, Decimal("3"))
        self.assertGreater(bands.upper, bands.middle)
        self.assertLess(bands.lower, bands.middle)
        self.assertGreater(bands.width, 0)

    def test_macd_length_and_defined_tail(self) -> None:
        result = macd(tuple(range(1, 50)))
        self.assertEqual(len(result), 49)
        self.assertIsNone(result[0])
        self.assertIsNotNone(result[-1])
        assert result[-1] is not None
        self.assertIsNotNone(result[-1].signal)

    def test_macd_rejects_reversed_periods(self) -> None:
        with self.assertRaisesRegex(AnalyticsError, "fast"):
            macd(tuple(range(1, 20)), fast_period=10, slow_period=5)

    def test_stochastic_bounds(self) -> None:
        points = stochastic_oscillator(candle_series(20), period=5, smooth=3)
        defined = [point for point in points if point]
        self.assertTrue(defined)
        self.assertTrue(all(Decimal("0") <= point.percent_k <= Decimal("100") for point in defined))

    def test_z_scores_for_flat_window(self) -> None:
        self.assertEqual(z_scores((5, 5, 5, 5), 3), (None, None, Decimal("0"), Decimal("0")))

    def test_percentile_interpolates(self) -> None:
        self.assertEqual(percentile((0, 10), Decimal("0.25")), Decimal("2.50"))
        self.assertEqual(percentile((10,), Decimal("0.9")), Decimal("10"))
        with self.assertRaises(AnalyticsError):
            percentile((1, 2), Decimal("1.1"))

    def test_linear_slope(self) -> None:
        self.assertEqual(linear_slope((1, 3, 5, 7)), Decimal("2"))
        self.assertEqual(linear_slope((4, 4, 4)), Decimal("0"))


class CandleIndicatorTests(unittest.TestCase):
    def test_true_range_uses_previous_close_gap(self) -> None:
        first = candle()
        second = replace(candle(1), low=first.close + Decimal("0.01"), open=first.close + Decimal("0.02"), close=first.close + Decimal("0.021"), high=first.close + Decimal("0.022"))
        ranges = true_range((first, second))
        self.assertEqual(ranges[1], second.high - first.close)

    def test_average_true_range(self) -> None:
        result = average_true_range(candle_series(20), 14)
        self.assertEqual(len(result), 20)
        self.assertTrue(all(value is None for value in result[:13]))
        self.assertGreater(result[-1], 0)  # type: ignore[operator]

    def test_vwap_ignores_zero_volume_until_available(self) -> None:
        values = tuple(replace(candle(index), volume=Decimal("0")) for index in range(2))
        with_volume = values + (candle(2, volume="10"),)
        result = volume_weighted_average_price(with_volume)
        self.assertEqual(result[:2], (None, None))
        self.assertIsNotNone(result[-1])


class AggregationTests(unittest.TestCase):
    def test_aggregate_quotes_builds_ohlcv(self) -> None:
        quotes = quote_series(5, step_seconds=60)
        result = aggregate_quotes(quotes, Timeframe.MINUTE_5)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].open, quotes[0].mid)
        self.assertEqual(result[0].close, quotes[-1].mid)
        self.assertEqual(result[0].sample_count, 5)
        self.assertEqual(result[0].volume, sum(item.volume for item in quotes))

    def test_price_selector_changes_prices(self) -> None:
        quotes = quote_series(2)
        bids = aggregate_quotes(quotes, Timeframe.MINUTE_5, price=bid_price)
        asks = aggregate_quotes(quotes, Timeframe.MINUTE_5, price=ask_price)
        self.assertEqual(bids[0].open, quotes[0].bid)
        self.assertEqual(asks[0].open, quotes[0].ask)

    def test_fill_gaps_uses_previous_close(self) -> None:
        quotes = quote_series(2, step_seconds=120)
        result = aggregate_quotes(quotes, Timeframe.MINUTE_1, include_empty=True)
        self.assertEqual(len(result), 3)
        self.assertEqual(result[1].open, result[0].close)
        self.assertEqual(result[1].volume, 0)

    def test_resample_hourly_to_four_hour(self) -> None:
        hourly = candle_series(8)
        result = resample_candles(hourly, Timeframe.HOUR_4)
        self.assertEqual(len(result), 2)
        self.assertTrue(all(item.complete for item in result))
        self.assertEqual(result[0].sample_count, 40)

    def test_resample_rejects_incompatible_target(self) -> None:
        with self.assertRaises(AnalyticsError):
            resample_candles(candle_series(2), Timeframe.MINUTE_30)

    def test_continuity_reports_gap(self) -> None:
        values = (candle(0), candle(2), candle(3))
        self.assertEqual(validate_candle_continuity(values), (BASE_TIME + timedelta(hours=1),))


class PerformanceTests(unittest.TestCase):
    def test_simple_and_log_returns(self) -> None:
        prices = (100, 110, 99)
        self.assertEqual(simple_returns(prices), (Decimal("0.1"), Decimal("-0.1")))
        logs = log_returns(prices)
        self.assertAlmostEqual(float(sum(logs)), float(Decimal("0.99").ln()))

    def test_cumulative_returns_compound(self) -> None:
        self.assertEqual(cumulative_returns((Decimal("0.1"), Decimal("-0.1")))[-1], Decimal("-0.01"))

    def test_drawdown_tracks_peak(self) -> None:
        returns = (Decimal("0.1"), Decimal("-0.2"), Decimal("0.1"))
        points = drawdown_series(returns)
        self.assertEqual(points[0].drawdown, 0)
        self.assertEqual(maximum_drawdown(returns), Decimal("-0.2"))

    def test_annualized_values(self) -> None:
        returns = (Decimal("0.01"),) * 12
        self.assertGreater(annualized_return(returns, 12), Decimal("0.12"))
        self.assertEqual(annualized_volatility(returns, 12), 0)

    def test_risk_ratios_handle_zero_deviation(self) -> None:
        returns = (Decimal("0.01"),) * 5
        self.assertIsNone(sharpe_ratio(returns))
        self.assertIsNone(sortino_ratio(returns))
        self.assertEqual(downside_deviation(returns), 0)

    def test_summary_counts_periods(self) -> None:
        summary = summarize_returns((Decimal("0.1"), Decimal("-0.1"), Decimal("0")))
        self.assertEqual(summary.winning_periods, 1)
        self.assertEqual(summary.losing_periods, 1)
        self.assertEqual(summary.flat_periods, 1)
        self.assertEqual(summary.win_rate, Decimal("0.3333333333333333333333333333"))


class SignalTests(unittest.TestCase):
    def test_policy_boundaries(self) -> None:
        policy = SignalPolicy()
        cases = {
            Decimal("80"): Signal.STRONG_BUY,
            Decimal("30"): Signal.BUY,
            Decimal("0"): Signal.NEUTRAL,
            Decimal("-30"): Signal.SELL,
            Decimal("-80"): Signal.STRONG_SELL,
        }
        for score, expected in cases.items():
            with self.subTest(score=score):
                self.assertIs(policy.signal(score), expected)

    def test_analysis_requires_history(self) -> None:
        service = AnalysisService(None)  # type: ignore[arg-type]
        with self.assertRaisesRegex(AnalyticsError, "35"):
            service.analyze_candles(candle_series(20))

    def test_analysis_returns_indicators_and_reason(self) -> None:
        service = AnalysisService(None)  # type: ignore[arg-type]
        result = service.analyze_candles(candle_series(50, alternating=True))
        self.assertEqual(result.symbol, "EURUSD")
        self.assertGreaterEqual(len(result.indicators), 10)
        self.assertTrue(result.reasons)
        self.assertGreaterEqual(result.score, Decimal("-100"))
        self.assertLessEqual(result.score, Decimal("100"))


if __name__ == "__main__":
    unittest.main()

