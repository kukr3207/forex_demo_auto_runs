"""Strategy composition and deterministic backtesting behavior."""

import unittest
from datetime import time
from decimal import Decimal

from forex_monitor.backtesting import (
    BacktestEngine,
    BacktestService,
    ExecutionModel,
    ParameterScore,
    SimulatedOrder,
    SimulationLedger,
    best_score,
    expanding_windows,
    parameter_grid,
    rank_scores,
    rolling_windows,
    trade_metrics,
)
from forex_monitor.models import Signal
from forex_monitor.portfolio import PositionSide
from forex_monitor.strategies import (
    ChannelBreakoutStrategy,
    MovingAverageTrendStrategy,
    RsiMomentumStrategy,
    SessionFilter,
    StrategyDecision,
    StrategyRegistry,
    StrategyService,
    VolatilityExpansionStrategy,
    ZScoreMeanReversionStrategy,
    combine_decisions,
    filter_session,
    filter_spread,
)
from tests.support import BASE_TIME, candle, candle_series


class AlternatingStrategy:
    @property
    def name(self) -> str:
        return "alternating"

    def evaluate(self, candles):  # type: ignore[no-untyped-def]
        signal = Signal.BUY if len(candles) % 2 else Signal.SELL
        return StrategyDecision(self.name, signal, Decimal("1"), ("test",), {})


class StrategyTests(unittest.TestCase):
    def test_decision_validation(self) -> None:
        decision = StrategyDecision("demo", Signal.BUY, Decimal("0.5"), ("reason",), {})
        self.assertEqual(decision.signal, Signal.BUY)
        with self.assertRaises(ValueError):
            StrategyDecision("", Signal.BUY, Decimal("0.5"), (), {})
        with self.assertRaises(ValueError):
            StrategyDecision("demo", Signal.BUY, Decimal("2"), (), {})

    def test_trend_momentum_and_mean_reversion(self) -> None:
        candles = candle_series(50)
        self.assertEqual(MovingAverageTrendStrategy().evaluate(candles).signal, Signal.BUY)
        self.assertIn(RsiMomentumStrategy().evaluate(candles).signal, tuple(Signal))
        self.assertIn(ZScoreMeanReversionStrategy().evaluate(candles).signal, tuple(Signal))
        with self.assertRaises(ValueError):
            MovingAverageTrendStrategy().evaluate(candles[:10])

    def test_breakout_and_volatility(self) -> None:
        candles = list(candle_series(30, alternating=True))
        high = candles[-1].high + Decimal("1")
        candles[-1] = candle(
            29,
            open_price=high,
            change="0.5",
        )
        self.assertEqual(ChannelBreakoutStrategy().evaluate(candles).signal, Signal.BUY)
        volatility = VolatilityExpansionStrategy(period=5, expansion_ratio=Decimal("0.1"))
        self.assertIn(volatility.evaluate(candles).signal, tuple(Signal))
        with self.assertRaises(ValueError):
            ChannelBreakoutStrategy().evaluate(candles[:5])

    def test_registry_service_and_ensemble(self) -> None:
        registry = StrategyRegistry()
        registry.register_all((MovingAverageTrendStrategy(), RsiMomentumStrategy()))
        self.assertEqual(registry.names(), ("moving_average_trend", "rsi_momentum"))
        service = StrategyService(registry)
        decisions = service.evaluate(candle_series(50), registry.names())
        self.assertEqual(len(decisions), 2)
        self.assertEqual(service.ensemble(candle_series(50), registry.names()).strategy, "ensemble")
        with self.assertRaises(ValueError):
            registry.register(MovingAverageTrendStrategy())
        with self.assertRaises(KeyError):
            registry.get("missing")

    def test_ensemble_neutral_and_directional(self) -> None:
        neutral = StrategyDecision("neutral", Signal.NEUTRAL, Decimal("0"), (), {})
        self.assertEqual(combine_decisions((neutral,)).signal, Signal.NEUTRAL)
        buys = (
            StrategyDecision("a", Signal.BUY, Decimal("1"), (), {}),
            StrategyDecision("b", Signal.STRONG_BUY, Decimal("1"), (), {}),
        )
        self.assertEqual(combine_decisions(buys).signal, Signal.BUY)
        with self.assertRaises(ValueError):
            combine_decisions(())

    def test_session_and_spread_filters(self) -> None:
        decision = StrategyDecision("demo", Signal.BUY, Decimal("1"), (), {})
        session = SessionFilter(time(8), time(17))
        self.assertTrue(session.allows(time(10)))
        self.assertEqual(filter_session(decision, session, time(18)).signal, Signal.NEUTRAL)
        self.assertEqual(filter_spread(decision, "0.1", "0.01").signal, Signal.NEUTRAL)
        overnight = SessionFilter(time(22), time(6))
        self.assertTrue(overnight.allows(time(23)))


class BacktestingTests(unittest.TestCase):
    def test_execution_and_ledger(self) -> None:
        model = ExecutionModel(Decimal("0.02"), Decimal("0.01"), Decimal("0.001"))
        entry_order = SimulatedOrder("EURUSD", PositionSide.LONG, Decimal("10"), BASE_TIME)
        entry = model.fill(entry_order, Decimal("1"), BASE_TIME)
        ledger = SimulationLedger(Decimal("1000"))
        ledger.open(entry)
        exit_order = SimulatedOrder("EURUSD", PositionSide.SHORT, Decimal("10"), BASE_TIME)
        trade = ledger.close(model.fill(exit_order, Decimal("1.20"), BASE_TIME))
        self.assertGreater(trade.pnl, 0)
        self.assertEqual(trade_metrics(ledger.completed()).winners, 1)
        with self.assertRaises(ValueError):
            ledger.close(entry)

    def test_engine_and_service(self) -> None:
        candles = candle_series(20, alternating=True)
        result = BacktestEngine().run(AlternatingStrategy(), candles, 1000, 10, 3)
        self.assertGreater(len(result.trades), 1)
        self.assertEqual(result.net_profit, result.final_balance - result.initial_balance)
        report = BacktestService().evaluate(AlternatingStrategy(), candles, 1000, 10, 3)
        self.assertEqual(report.metrics.count, len(report.result.trades))
        with self.assertRaises(ValueError):
            BacktestEngine().run(AlternatingStrategy(), candles, 0, 1, 3)

    def test_walk_forward_windows(self) -> None:
        rolling = rolling_windows(100, 40, 10)
        expanding = expanding_windows(100, 40, 10)
        self.assertEqual(len(rolling), 6)
        self.assertEqual(expanding[-1].train_start, 0)
        with self.assertRaises(ValueError):
            rolling_windows(10, 0, 2)

    def test_parameter_optimization(self) -> None:
        grid = parameter_grid({"slow": (20, 30), "fast": (5, 10)})
        self.assertEqual(len(grid), 4)
        scores = (
            ParameterScore(grid[0], Decimal("1")),
            ParameterScore(grid[1], Decimal("2")),
        )
        self.assertEqual(best_score(scores).score, Decimal("2"))
        self.assertEqual(rank_scores(scores)[0].score, Decimal("2"))
        with self.assertRaises(ValueError):
            parameter_grid({})
        with self.assertRaises(ValueError):
            best_score(())


if __name__ == "__main__":
    unittest.main()
