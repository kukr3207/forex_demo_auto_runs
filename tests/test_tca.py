from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from forex_monitor.errors import StorageConflictError, ValidationError
from forex_monitor.execution.fills import Fill
from forex_monitor.execution.orders import OrderSide
from forex_monitor.tca.analysis import ExecutionAnalyzer, percentile
from forex_monitor.tca.benchmarks import BenchmarkBook, BenchmarkKind, MarketObservation
from forex_monitor.tca.reports import ScorecardBuilder, scorecard_as_dict


NOW = datetime(2026, 1, 5, 12, tzinfo=timezone.utc)


class BenchmarkTests(unittest.TestCase):
    def observation(self, index=0, **changes):
        values = dict(
            observation_id=f"obs-{index}", symbol="EURUSD",
            observed_at=NOW + timedelta(minutes=index),
            bid=Decimal("1.0998") + Decimal(index) / Decimal("10000"),
            ask=Decimal("1.1002") + Decimal(index) / Decimal("10000"),
            traded_volume=Decimal(100 + index * 100), source="feed",
        )
        values.update(changes)
        return MarketObservation(**values)

    def test_record_is_idempotent_and_conflicts_are_rejected(self):
        book = BenchmarkBook()
        value = self.observation()
        self.assertIs(book.record(value), book.record(value))
        with self.assertRaises(StorageConflictError):
            book.record(self.observation(0, ask="1.2"))

    def test_twap_vwap_and_arrival_benchmarks(self):
        book = BenchmarkBook()
        book.record_many(self.observation(index) for index in range(3))
        twap = book.calculate("EURUSD", BenchmarkKind.TWAP, NOW, NOW + timedelta(minutes=2))
        vwap = book.calculate("EURUSD", BenchmarkKind.VWAP, NOW, NOW + timedelta(minutes=2))
        arrival = book.calculate("EURUSD", BenchmarkKind.ARRIVAL_SIDE, NOW, NOW, side="buy")
        self.assertEqual(twap.price, Decimal("1.1001"))
        self.assertGreater(vwap.price, twap.price)
        self.assertEqual(arrival.price, Decimal("1.1002"))
        self.assertEqual(twap.observation_count, 3)

    def test_vwap_requires_volume_and_window_requires_data(self):
        book = BenchmarkBook()
        book.record(self.observation(traded_volume=0))
        with self.assertRaises(ValidationError):
            book.calculate("EURUSD", BenchmarkKind.VWAP, NOW, NOW)
        with self.assertRaises(ValidationError):
            book.calculate("GBPUSD", BenchmarkKind.TWAP, NOW, NOW)


class ExecutionAnalysisTests(unittest.TestCase):
    def benchmark(self, price="1.1"):
        from forex_monitor.tca.benchmarks import BenchmarkValue
        return BenchmarkValue("EURUSD", BenchmarkKind.ARRIVAL_MID, Decimal(price), NOW, NOW, 1, Decimal("100"))

    def fill(self, fill_id, price, quantity="50", side=OrderSide.BUY):
        return Fill(fill_id, "order", "EURUSD", side, quantity, price, NOW, "venue", commission="1")

    def test_buy_slippage_and_implementation_shortfall(self):
        analysis = ExecutionAnalyzer.analyze_order(
            "order", "EURUSD", OrderSide.BUY, "100",
            (self.fill("one", "1.101"), self.fill("two", "1.102")),
            self.benchmark(),
        )
        self.assertEqual(analysis.average_execution_price, Decimal("1.1015"))
        self.assertEqual(analysis.execution_cost, Decimal("0.150"))
        self.assertEqual(analysis.opportunity_cost, Decimal("0.0"))
        self.assertEqual(analysis.commissions, Decimal("2"))
        self.assertEqual(analysis.completion_rate, Decimal("1"))

    def test_sell_slippage_has_opposite_sign(self):
        cost = ExecutionAnalyzer.analyze_fill(self.fill("one", "1.099", side=OrderSide.SELL), self.benchmark())
        self.assertEqual(cost.slippage, Decimal("0.001"))
        self.assertGreater(cost.slippage_bps, 0)

    def test_partial_order_includes_opportunity_cost(self):
        analysis = ExecutionAnalyzer.analyze_order(
            "order", "EURUSD", OrderSide.BUY, "100",
            (self.fill("one", "1.101", quantity="40"),),
            self.benchmark("1.102"), decision_price="1.1",
        )
        self.assertEqual(analysis.unexecuted_quantity, Decimal("60"))
        self.assertEqual(analysis.opportunity_cost, Decimal("0.120"))

    def test_mismatched_and_overfilled_orders_are_rejected(self):
        with self.assertRaises(ValidationError):
            ExecutionAnalyzer.analyze_order("other", "EURUSD", OrderSide.BUY, 100, (self.fill("one", "1.1"),), self.benchmark())
        with self.assertRaises(ValidationError):
            ExecutionAnalyzer.analyze_order("order", "EURUSD", OrderSide.BUY, 10, (self.fill("one", "1.1"),), self.benchmark())

    def test_percentile_interpolates_deterministically(self):
        self.assertEqual(percentile((1, 2, 3, 4), Decimal("0.5")), Decimal("2.5"))
        self.assertEqual(percentile((1, 2, 3, 4), Decimal("0.9")), Decimal("3.7"))

    def test_scorecards_weight_by_order_quantity(self):
        first = ExecutionAnalyzer.analyze_order("order", "EURUSD", OrderSide.BUY, 100, (self.fill("one", "1.101", quantity="100"),), self.benchmark())
        second_fill = Fill("two", "second", "EURUSD", OrderSide.BUY, "300", "1.102", NOW, "venue", commission="2")
        second = ExecutionAnalyzer.analyze_order("second", "EURUSD", OrderSide.BUY, 300, (second_fill,), self.benchmark())
        card = ScorecardBuilder.by_symbol((first, second))[0]
        self.assertEqual(card.order_count, 2)
        self.assertEqual(card.executed_quantity, Decimal("400"))
        self.assertGreater(card.average_shortfall_bps, first.implementation_shortfall_bps)
        self.assertEqual(scorecard_as_dict(card)["orderCount"], 2)


if __name__ == "__main__":
    unittest.main()

