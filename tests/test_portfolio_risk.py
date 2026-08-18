"""Portfolio valuation and risk planning behavior."""

import unittest
from decimal import Decimal

from forex_monitor.errors import ValidationError
from forex_monitor.portfolio import (
    AccountSnapshot,
    PortfolioService,
    Position,
    PositionSide,
    concentration_index,
    currency_exposures,
    gross_leverage,
    largest_exposure,
    normalize_weights,
    position_contributions,
    rebalance_plan,
)
from forex_monitor.risk import (
    RiskLimits,
    RiskService,
    apply_scenario,
    atr_stop_distance,
    correlation,
    correlation_pairs,
    drawdown_series,
    evaluate_order,
    exit_levels,
    expected_shortfall,
    fixed_fractional_size,
    historical_var,
    maximum_drawdown,
    risk_amount,
    scenario_matrix,
)


class PositionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.long = Position(
            "eur/usd", PositionSide.LONG, Decimal("100"), Decimal("1.10"), Decimal("1.12")
        )
        self.short = Position(
            "GBPUSD", PositionSide.SHORT, Decimal("50"), Decimal("1.30"), Decimal("1.25")
        )

    def test_position_normalizes_and_values(self) -> None:
        self.assertEqual(self.long.symbol, "EURUSD")
        self.assertEqual(self.long.notional, Decimal("112"))
        self.assertEqual(self.long.unrealized_pnl, Decimal("2.00"))
        self.assertEqual(self.short.unrealized_pnl, Decimal("2.50"))
        self.assertEqual(self.long.mark("1.15").current_price, Decimal("1.15"))

    def test_position_rejects_invalid_values(self) -> None:
        with self.assertRaises(ValidationError):
            Position("$bad", PositionSide.LONG, Decimal("1"), Decimal("1"), Decimal("1"))
        with self.assertRaises(ValidationError):
            self.long.mark("0")

    def test_account_margin_properties(self) -> None:
        account = AccountSnapshot(Decimal("1000"), Decimal("50"), Decimal("200"))
        self.assertEqual(account.equity, Decimal("1050"))
        self.assertEqual(account.free_margin, Decimal("850"))
        self.assertEqual(account.margin_level, Decimal("525"))
        self.assertTrue(AccountSnapshot(Decimal("1")).margin_level.is_infinite())
        with self.assertRaises(ValueError):
            AccountSnapshot(Decimal("-1"))

    def test_portfolio_service_marks_prices(self) -> None:
        result = PortfolioService().snapshot("1000", (self.long,), {"EURUSD": "1.20"}, "100")
        self.assertEqual(result.positions[0].current_price, Decimal("1.20"))
        self.assertEqual(result.account.unrealized_pnl, Decimal("10.00"))
        self.assertTrue(result.exposures)
        self.assertEqual(
            gross_leverage(result.positions, result.account.equity),
            Decimal("120") / Decimal("1010"),
        )

    def test_currency_exposure_and_concentration(self) -> None:
        exposures = currency_exposures((self.long, self.short))
        self.assertEqual(tuple(item.currency for item in exposures), ("EUR", "GBP", "USD"))
        self.assertEqual(largest_exposure((self.long, self.short)).currency, "EUR")
        contributions = position_contributions((self.long, self.short))
        self.assertEqual(
            sum((item.notional_weight for item in contributions), Decimal("0")), Decimal("1")
        )
        self.assertGreater(concentration_index((self.long, self.short)), Decimal("0.5"))
        with self.assertRaises(ValueError):
            largest_exposure(())

    def test_allocations_and_rebalance(self) -> None:
        weights = normalize_weights({"EURUSD": 2, "GBPUSD": 1})
        self.assertEqual(weights["EURUSD"], Decimal("2") / Decimal("3"))
        plan = rebalance_plan({"EURUSD": 100}, weights, 900)
        self.assertEqual(len(plan), 2)
        self.assertEqual(plan[0].difference, Decimal("500"))
        with self.assertRaises(ValueError):
            normalize_weights({"EURUSD": 0})
        with self.assertRaises(ValueError):
            normalize_weights({"EURUSD": -1})


class RiskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.limits = RiskLimits(Decimal("50000"), Decimal("3"), Decimal("500"), 4)

    def test_sizing_and_risk_amount(self) -> None:
        self.assertEqual(fixed_fractional_size(10000, "0.01", "0.005"), Decimal("20000"))
        self.assertEqual(risk_amount(10000, "0.01"), Decimal("100.00"))
        with self.assertRaises(ValueError):
            fixed_fractional_size(10000, "1.1", "0.1")

    def test_exit_levels_for_both_sides(self) -> None:
        long = exit_levels("1.20", PositionSide.LONG, "0.01", "2")
        short = exit_levels("1.20", PositionSide.SHORT, "0.01", "2")
        self.assertEqual((long.stop_loss, long.take_profit), (Decimal("1.19"), Decimal("1.22")))
        self.assertEqual((short.stop_loss, short.take_profit), (Decimal("1.21"), Decimal("1.18")))
        self.assertEqual(atr_stop_distance("0.005", "3"), Decimal("0.015"))

    def test_limit_decisions(self) -> None:
        allowed = evaluate_order(self.limits, 1000, 1, -10, 1)
        denied = evaluate_order(self.limits, 60000, 4, -600, 4)
        self.assertTrue(allowed.allowed)
        self.assertEqual(
            denied.reasons,
            ("order_notional", "gross_leverage", "daily_loss", "open_positions"),
        )
        with self.assertRaises(ValueError):
            RiskLimits(Decimal("1"), Decimal("1"), Decimal("1"), 0)

    def test_risk_service_composes_plan(self) -> None:
        plan = RiskService(self.limits).plan(
            10000, "0.001", "1.10", "0.01", PositionSide.LONG, 1, 0, 1
        )
        self.assertGreater(plan.quantity, 0)
        self.assertTrue(plan.decision.allowed)

    def test_drawdown_and_tail_risk(self) -> None:
        points = drawdown_series((100, 120, 90, 110))
        self.assertEqual(points[-2].drawdown, Decimal("0.25"))
        self.assertEqual(maximum_drawdown((100, 120, 90)), Decimal("0.25"))
        self.assertEqual(maximum_drawdown(()), Decimal("0"))
        returns = ("-0.10", "-0.05", "0.01", "0.02", "0.03")
        self.assertGreater(historical_var(returns, "0.8"), 0)
        self.assertGreaterEqual(expected_shortfall(returns, "0.8"), historical_var(returns, "0.8"))
        with self.assertRaises(ValueError):
            historical_var((), "0.95")

    def test_correlation_helpers(self) -> None:
        self.assertEqual(correlation((1, 2, 3), (2, 4, 6)), Decimal("1"))
        self.assertEqual(correlation((1, 1), (2, 3)), Decimal("0"))
        pairs = correlation_pairs({"a": (1, 2, 3), "b": (2, 4, 6), "c": (3, 2, 1)})
        self.assertEqual(len(pairs), 3)
        with self.assertRaises(ValueError):
            correlation((1,), (1,))

    def test_scenarios(self) -> None:
        position = Position("EURUSD", PositionSide.LONG, Decimal("100"), Decimal("1"), Decimal("1"))
        result = apply_scenario("rise", (position,), {"EURUSD": "0.1"})
        self.assertEqual(result.pnl, Decimal("10.0"))
        self.assertEqual(
            len(
                scenario_matrix(
                    (position,), {"fall": {"EURUSD": "-0.1"}, "rise": {"EURUSD": "0.1"}}
                )
            ),
            2,
        )
        with self.assertRaises(ValueError):
            apply_scenario("", (position,), {})
        with self.assertRaises(ValueError):
            apply_scenario("bad", (position,), {"EURUSD": "-1"})


if __name__ == "__main__":
    unittest.main()
