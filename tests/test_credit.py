from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import unittest

from forex_monitor.credit.counterparties import Counterparty, CounterpartyBook, CounterpartyState, CreditLimit
from forex_monitor.credit.exposure import CreditExposureEngine, MarkToMarket, NettingAgreement
from forex_monitor.credit.xva import ExposurePoint, HazardCurve, HazardPoint, RecoveryAssumption, XvaCalculator
from forex_monitor.errors import StorageConflictError, ValidationError


NOW = datetime(2026, 1, 5, 12, tzinfo=timezone.utc)


class CounterpartyTests(unittest.TestCase):
    def counterparty(self, **changes):
        values = dict(
            counterparty_id="bank", legal_name="Example Bank", lei="5493001KJTIIGC8Y1R12",
            country="GB", rating="A", onboarding_date=date(2025, 1, 1),
        )
        values.update(changes)
        return Counterparty(**values)

    def test_registration_lei_uniqueness_and_state_versions(self):
        book = CounterpartyBook()
        value = self.counterparty()
        self.assertIs(book.register(value), book.register(value))
        with self.assertRaises(StorageConflictError):
            book.register(self.counterparty(counterparty_id="other"))
        active = book.transition("bank", 1, CounterpartyState.ACTIVE)
        self.assertEqual(active.version, 2)
        with self.assertRaises(StorageConflictError):
            book.transition("bank", 1, CounterpartyState.SUSPENDED)

    def test_effective_limits_apply_most_restrictive_scope(self):
        book = CounterpartyBook()
        book.register(self.counterparty(state=CounterpartyState.ACTIVE))
        book.add_limit(CreditLimit("all", "bank", "USD", "1000000", NOW, approved_by="risk"))
        book.add_limit(CreditLimit("spot", "bank", "USD", "250000", NOW, product="spot", approved_by="risk"))
        self.assertEqual(book.available_limit("bank", "USD", "50000", NOW, "spot"), Decimal("200000"))
        self.assertEqual(book.available_limit("bank", "USD", "50000", NOW, "forward"), Decimal("950000"))

    def test_nonactive_counterparty_has_no_available_limit(self):
        book = CounterpartyBook()
        book.register(self.counterparty())
        book.add_limit(CreditLimit("limit", "bank", "USD", "100", NOW, approved_by="risk"))
        self.assertEqual(book.available_limit("bank", "USD", 0, NOW), 0)

    def test_overlapping_limit_scope_is_rejected(self):
        book = CounterpartyBook()
        book.register(self.counterparty())
        book.add_limit(CreditLimit("one", "bank", "USD", 100, NOW, approved_by="risk"))
        with self.assertRaises(ValidationError):
            book.add_limit(CreditLimit("two", "bank", "USD", 200, NOW + timedelta(days=1), approved_by="risk"))


class ExposureTests(unittest.TestCase):
    def test_enforceable_agreement_nets_positive_and_negative_trades(self):
        engine = CreditExposureEngine("USD")
        agreement = engine.add_agreement(NettingAgreement("csa", "bank", "ISDA", "USD", NOW.date(), independent_amount="10"))
        engine.value(MarkToMarket("one", "bank", agreement.agreement_id, "USD", "100", NOW, date(2027, 1, 1), "forward"))
        engine.value(MarkToMarket("two", "bank", agreement.agreement_id, "USD", "-60", NOW, date(2027, 1, 1), "swap"))
        engine.set_collateral("bank", "USD", "20")
        profile = engine.profile("bank", NOW, {})
        self.assertEqual(profile.gross_positive, Decimal("100"))
        self.assertEqual(profile.gross_negative, Decimal("-60"))
        self.assertEqual(profile.current_exposure, Decimal("40"))
        self.assertEqual(profile.unsecured_exposure, Decimal("30"))

    def test_unenforceable_agreement_does_not_net_trades(self):
        engine = CreditExposureEngine()
        engine.add_agreement(NettingAgreement("a", "bank", "ISDA", "USD", NOW.date(), netting_enforceable=False))
        engine.value(MarkToMarket("one", "bank", "a", "USD", 100, NOW, date(2027, 1, 1), "forward"))
        engine.value(MarkToMarket("two", "bank", "a", "USD", -80, NOW, date(2027, 1, 1), "forward"))
        self.assertEqual(engine.profile("bank", NOW, {}).current_exposure, Decimal("100"))

    def test_currency_conversion_accepts_direct_and_inverse_rates(self):
        engine = CreditExposureEngine("USD")
        engine.value(MarkToMarket("eur", "bank", None, "EUR", 100, NOW, date(2027, 1, 1), "forward"))
        self.assertEqual(engine.profile("bank", NOW, {"EURUSD": "1.2"}).current_exposure, Decimal("120.0"))
        self.assertEqual(engine.profile("bank", NOW, {"USDEUR": "0.8"}).current_exposure, Decimal("125"))


class XvaTests(unittest.TestCase):
    def test_xva_uses_incremental_default_probabilities(self):
        exposures = (
            ExposurePoint(Decimal("1"), Decimal("100"), Decimal("20"), Decimal("0.98"), Decimal("50")),
            ExposurePoint(Decimal("2"), Decimal("80"), Decimal("25"), Decimal("0.95"), Decimal("40")),
        )
        counterparty = HazardCurve((HazardPoint(Decimal("1"), Decimal("0.98")), HazardPoint(Decimal("2"), Decimal("0.95"))))
        own = HazardCurve((HazardPoint(Decimal("1"), Decimal("0.99")), HazardPoint(Decimal("2"), Decimal("0.97"))))
        result = XvaCalculator.calculate(exposures, counterparty, own, RecoveryAssumption(Decimal("0.4"), Decimal("0.4")), Decimal("0.01"))
        self.assertGreater(result.cva, result.dva)
        self.assertGreater(result.fva, 0)
        self.assertEqual(result.expected_positive_exposure, Decimal("90"))
        self.assertEqual(result.total_adjustment, -result.cva + result.dva - result.fva)

    def test_curve_grid_must_match_exposure_grid(self):
        exposures = (ExposurePoint(Decimal("1"), 1, 1, 1),)
        counterparty = HazardCurve((HazardPoint(Decimal("2"), Decimal("0.9")),))
        own = HazardCurve((HazardPoint(Decimal("1"), Decimal("0.9")),))
        with self.assertRaises(ValidationError):
            XvaCalculator.calculate(exposures, counterparty, own, RecoveryAssumption(Decimal("0.4"), Decimal("0.4")), 0)


if __name__ == "__main__":
    unittest.main()

