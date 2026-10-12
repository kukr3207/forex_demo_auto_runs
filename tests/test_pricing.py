from datetime import date, datetime, timezone
from decimal import Decimal
import unittest

from forex_monitor.errors import ValidationError
from forex_monitor.pricing.conventions import (
    BusinessDayRule,
    CurrencyConvention,
    HolidayCalendar,
    MarketConventions,
    PairConvention,
)
from forex_monitor.pricing.curves import DiscountCurve, DiscountNode, FxForwardCurve
from forex_monitor.pricing.day_count import DayCount, actual_days, year_fraction
from forex_monitor.pricing.forwards import (
    ForwardContract,
    ForwardSide,
    break_even_spot,
    forward_cashflows,
    forward_delta,
    forward_dv01,
    price_forward,
)
from forex_monitor.pricing.ndf import (
    NdfContract,
    NdfFixing,
    NdfSettlementFormula,
    indicative_settlement,
    settle_ndf,
)
from forex_monitor.pricing.options import (
    FxOption,
    OptionType,
    implied_volatility,
    option_payoff,
    price_option,
)
from forex_monitor.pricing.portfolio import PricingBook, convert_value
from forex_monitor.pricing.swaps import FxSwap, price_swap, roll_swap, swap_cashflows
from forex_monitor.pricing.volatility import SmilePoint, VolatilitySmile, VolatilitySurface


class DayCountTests(unittest.TestCase):
    def test_actual_conventions(self):
        start = date(2024, 1, 1)
        end = date(2024, 7, 1)
        self.assertEqual(actual_days(start, end), 182)
        self.assertEqual(year_fraction(start, end, DayCount.ACT_360), Decimal(182) / Decimal(360))
        self.assertEqual(year_fraction(start, end, "ACT365F"), Decimal(182) / Decimal(365))

    def test_actual_actual_splits_leap_years(self):
        value = year_fraction(date(2023, 7, 1), date(2024, 7, 1), DayCount.ACT_ACT)
        expected = Decimal(184) / Decimal(365) + Decimal(182) / Decimal(366)
        self.assertEqual(value, expected)

    def test_thirty_day_conventions(self):
        start = date(2024, 1, 31)
        end = date(2024, 2, 29)
        self.assertEqual(year_fraction(start, end, DayCount.THIRTY_E_360), Decimal(29) / Decimal(360))
        self.assertEqual(year_fraction(start, end, DayCount.THIRTY_360_US), Decimal(29) / Decimal(360))

    def test_invalid_boundaries_are_rejected(self):
        with self.assertRaises(ValidationError):
            year_fraction(date(2024, 2, 1), date(2024, 1, 1), DayCount.ACT_360)
        with self.assertRaises(ValidationError):
            DayCount.parse("unknown")


class ConventionTests(unittest.TestCase):
    def test_calendar_adjust_and_shift(self):
        calendar = HolidayCalendar("London", (date(2024, 1, 1),))
        self.assertEqual(calendar.shift(date(2023, 12, 29), 1), date(2024, 1, 2))
        self.assertEqual(
            calendar.adjust(date(2024, 3, 31), BusinessDayRule.MODIFIED_FOLLOWING),
            date(2024, 3, 29),
        )

    def test_standard_registry_calculates_joint_spot_date(self):
        conventions = MarketConventions.standard()
        self.assertEqual(conventions.spot_date("EUR/USD", date(2024, 1, 5)), date(2024, 1, 9))
        self.assertEqual(conventions.pair("USDJPY").pip_size, Decimal("0.01"))

    def test_registry_rejects_unknown_dependencies(self):
        conventions = MarketConventions()
        with self.assertRaises(ValidationError):
            conventions.add_currency(CurrencyConvention("USD", default_calendar="NYC"))
        conventions.add_currency(CurrencyConvention("USD"))
        with self.assertRaises(ValidationError):
            conventions.add_pair(PairConvention("EURUSD", "EUR", "USD", Decimal("0.0001")))


class CurveTests(unittest.TestCase):
    def setUp(self):
        self.valuation = date(2024, 1, 2)
        self.domestic = DiscountCurve(
            "USD",
            self.valuation,
            (
                DiscountNode(date(2024, 7, 2), Decimal("0.975")),
                DiscountNode(date(2025, 1, 2), Decimal("0.95")),
                DiscountNode(date(2026, 1, 2), Decimal("0.90")),
            ),
        )
        self.foreign = DiscountCurve(
            "EUR",
            self.valuation,
            (
                DiscountNode(date(2024, 7, 2), Decimal("0.985")),
                DiscountNode(date(2025, 1, 2), Decimal("0.97")),
                DiscountNode(date(2026, 1, 2), Decimal("0.94")),
            ),
        )
        self.fx = FxForwardCurve("EURUSD", Decimal("1.10"), self.domestic, self.foreign)

    def test_exact_interpolated_and_extrapolated_discounts(self):
        self.assertEqual(self.domestic.discount(date(2024, 7, 2)), Decimal("0.975"))
        middle = self.domestic.discount(date(2024, 10, 2))
        self.assertGreater(middle, Decimal("0.95"))
        self.assertLess(middle, Decimal("0.975"))
        self.assertLess(self.domestic.discount(date(2027, 1, 2)), Decimal("0.90"))

    def test_forward_parity_and_points(self):
        maturity = date(2025, 1, 2)
        expected = Decimal("1.10") * Decimal("0.97") / Decimal("0.95")
        self.assertEqual(self.fx.forward(maturity), expected)
        self.assertEqual(self.fx.points(maturity), (expected - Decimal("1.10")) / Decimal("0.0001"))

    def test_curve_bump_changes_zero_rate(self):
        bumped = self.domestic.bump(Decimal("1"))
        self.assertGreater(bumped.zero_rate(date(2025, 1, 2)), self.domestic.zero_rate(date(2025, 1, 2)))

    def test_curve_validation(self):
        with self.assertRaises(ValidationError):
            DiscountCurve("USD", self.valuation, ())
        with self.assertRaises(ValidationError):
            DiscountCurve(
                "USD",
                self.valuation,
                (
                    DiscountNode(date(2024, 7, 2), Decimal("0.95")),
                    DiscountNode(date(2025, 1, 2), Decimal("0.96")),
                ),
            )


class ProductPricingTests(CurveTests):
    def test_forward_value_cashflows_and_risk(self):
        contract = ForwardContract(
            "fwd-1", "EURUSD", ForwardSide.BUY_BASE, Decimal("1000000"),
            Decimal("1.11"), date(2025, 1, 2), self.valuation,
        )
        quote = price_forward(contract, self.fx)
        self.assertEqual(quote.currency, "USD")
        self.assertEqual(quote.settlement_value, contract.notional * (quote.fair_rate - contract.strike))
        self.assertEqual(forward_cashflows(contract)["EUR"], Decimal("1000000"))
        self.assertGreater(forward_delta(contract, self.fx), 0)
        self.assertNotEqual(forward_dv01(contract, self.fx, domestic=True), 0)
        self.assertGreater(break_even_spot(contract, self.fx), 0)

    def test_swap_combines_near_and_far_legs(self):
        swap = FxSwap(
            "swap-1", "EURUSD", ForwardSide.BUY_BASE, Decimal("2000000"),
            Decimal("1.105"), Decimal("1.12"), date(2024, 7, 2),
            date(2025, 1, 2), self.valuation,
        )
        quote = price_swap(swap, self.fx)
        self.assertEqual(quote.present_value, quote.near_present_value + quote.far_present_value)
        self.assertEqual(len(swap_cashflows(swap)), 2)
        rolled = roll_swap(swap, date(2026, 1, 2), self.fx)
        self.assertEqual(rolled.near_date, swap.far_date)

    def test_ndf_quote_and_base_settlement(self):
        fixing_time = datetime(2025, 1, 2, 16, tzinfo=timezone.utc)
        quote_contract = NdfContract(
            "ndf-q", "USDBRL", Decimal("1000000"), Decimal("5.00"),
            fixing_time.date(), date(2025, 1, 6), "BRL",
        )
        fixing = NdfFixing("USDBRL", Decimal("5.10"), fixing_time, "PTAX")
        result = settle_ndf(quote_contract, fixing)
        self.assertEqual(result.amount, Decimal("100000"))
        base_contract = NdfContract(
            "ndf-b", "USDBRL", Decimal("1000000"), Decimal("5.00"),
            fixing_time.date(), date(2025, 1, 6), "USD",
            NdfSettlementFormula.BASE_CURRENCY,
        )
        self.assertEqual(settle_ndf(base_contract, fixing).amount, Decimal("100000") / Decimal("5.10"))
        self.assertEqual(indicative_settlement(quote_contract, Decimal("5.10")), Decimal("100000"))

    def test_option_price_greeks_and_implied_volatility(self):
        option = FxOption(
            "opt-1", "EURUSD", OptionType.CALL, Decimal("1000000"),
            Decimal("1.10"), date(2025, 1, 2), date(2025, 1, 6),
        )
        quote = price_option(option, self.fx, Decimal("0.12"))
        self.assertGreater(quote.value, 0)
        self.assertGreater(quote.delta, 0)
        self.assertGreater(quote.gamma, 0)
        self.assertGreater(quote.vega, 0)
        implied = implied_volatility(option, self.fx, quote.unit_price)
        self.assertAlmostEqual(float(implied), 0.12, places=5)
        self.assertEqual(option_payoff(option, Decimal("1.15")), Decimal("50000"))


class VolatilityTests(unittest.TestCase):
    def setUp(self):
        self.valuation = date(2024, 1, 2)
        self.smile_one = VolatilitySmile(
            "EURUSD", date(2024, 7, 2),
            (
                SmilePoint(Decimal("0.10"), Decimal("0.14")),
                SmilePoint(Decimal("0.25"), Decimal("0.12")),
                SmilePoint(Decimal("0.50"), Decimal("0.11")),
                SmilePoint(Decimal("0.75"), Decimal("0.115")),
                SmilePoint(Decimal("0.90"), Decimal("0.13")),
            ),
        )
        self.smile_two = VolatilitySmile(
            "EURUSD", date(2025, 1, 2),
            (
                SmilePoint(Decimal("0.10"), Decimal("0.15")),
                SmilePoint(Decimal("0.25"), Decimal("0.13")),
                SmilePoint(Decimal("0.50"), Decimal("0.12")),
                SmilePoint(Decimal("0.75"), Decimal("0.125")),
                SmilePoint(Decimal("0.90"), Decimal("0.14")),
            ),
        )

    def test_smile_interpolation_and_metrics(self):
        value = self.smile_one.volatility(Decimal("0.375"))
        self.assertEqual(value, Decimal("0.115"))
        self.assertEqual(self.smile_one.atm, Decimal("0.11"))
        self.assertEqual(self.smile_one.risk_reversal(), Decimal("-0.005"))
        self.assertEqual(self.smile_one.butterfly(), Decimal("0.0075"))

    def test_surface_interpolates_total_variance(self):
        surface = VolatilitySurface("EURUSD", self.valuation, (self.smile_one, self.smile_two))
        value = surface.volatility(date(2024, 10, 2), Decimal("0.5"))
        self.assertGreater(value, Decimal("0.11"))
        self.assertLess(value, Decimal("0.12"))
        self.assertEqual(surface.calendar_arbitrage((Decimal("0.25"),)), ())


class PricingBookTests(CurveTests):
    def test_book_values_multiple_instruments(self):
        forward = ForwardContract(
            "fwd", "EURUSD", ForwardSide.BUY_BASE, Decimal("100000"),
            Decimal("1.1"), date(2025, 1, 2), self.valuation,
        )
        option = FxOption(
            "opt", "EURUSD", OptionType.PUT, Decimal("100000"),
            Decimal("1.12"), date(2025, 1, 2), date(2025, 1, 6),
        )
        book = PricingBook("USD")
        book.add(forward)
        book.add(option)
        report = book.value({"EURUSD": self.fx}, volatilities={"opt": Decimal("0.12")})
        self.assertEqual(len(report.lines), 2)
        self.assertEqual(report.total, sum((line.reporting_value for line in report.lines), Decimal("0")))
        self.assertEqual(set(report.by_type), {"forward", "option"})
        self.assertIn("total", report.as_dict())

    def test_conversion_accepts_direct_and_inverse_rates(self):
        self.assertEqual(convert_value(Decimal("10"), "EUR", "USD", {"EURUSD": "1.1"}), Decimal("11"))
        self.assertEqual(convert_value(Decimal("11"), "USD", "EUR", {"EURUSD": "1.1"}), Decimal("10"))
        with self.assertRaises(ValidationError):
            convert_value(Decimal("1"), "EUR", "JPY", {})


if __name__ == "__main__":
    unittest.main()
