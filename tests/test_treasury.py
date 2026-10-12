from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import unittest

from forex_monitor.errors import StorageConflictError, ValidationError
from forex_monitor.treasury.cash import CashAccount, CashLedger, CashMovement, MovementKind
from forex_monitor.treasury.collateral import (
    CollateralAsset, CollateralPool, CollateralType, MarginCall, MarginCallState,
)
from forex_monitor.treasury.funding import (
    FundingDesk, FundingInstrument, FundingQuote, FundingSide,
)
from forex_monitor.treasury.liquidity import (
    CashflowConfidence, ForecastCashflow, LiquidityPlanner,
)
from forex_monitor.treasury.netting import NettingEngine, PaymentObligation
from forex_monitor.treasury.settlement import (
    SettlementBook, SettlementInstruction, SettlementLeg, SettlementState,
)


NOW = datetime(2026, 2, 2, 10, tzinfo=timezone.utc)
TODAY = NOW.date()


class CashLedgerTests(unittest.TestCase):
    def setUp(self):
        self.ledger = CashLedger()
        self.ledger.open(CashAccount("house", "USD", "treasury", overdraft_limit="1000"), "500")
        self.ledger.open(CashAccount("client", "USD", "client"), "100")

    def movement(self, identifier="m1", amount="200"):
        return CashMovement(
            identifier, "house", "client", Decimal(amount), "USD", TODAY,
            NOW, MovementKind.TRANSFER, "transfer-ref",
        )

    def test_double_entry_booking_and_idempotency(self):
        movement = self.movement()
        self.assertIs(self.ledger.book(movement), self.ledger.book(movement))
        self.assertEqual(self.ledger.balance("house"), Decimal("300"))
        self.assertEqual(self.ledger.balance("client"), Decimal("300"))
        self.assertEqual(self.ledger.trial_balance()["USD"], Decimal("600"))
        with self.assertRaises(StorageConflictError):
            self.ledger.book(self.movement(amount="201"))

    def test_atomic_batch_rejects_overdraft_without_partial_write(self):
        movements = (
            self.movement("m1", "500"),
            self.movement("m2", "1001"),
        )
        with self.assertRaises(ValidationError):
            self.ledger.book_batch(movements)
        self.assertEqual(self.ledger.balance("house"), Decimal("500"))
        self.assertEqual(self.ledger.movements("house"), ())

    def test_snapshot_filters_by_value_date(self):
        self.ledger.book(self.movement())
        future = CashMovement(
            "m2", "client", "house", Decimal("50"), "USD", TODAY + timedelta(days=1),
            NOW, MovementKind.TRADE, "future",
        )
        self.ledger.book(future)
        snapshot = self.ledger.snapshot("house", TODAY)
        self.assertEqual(snapshot.booked_balance, Decimal("-200"))
        self.assertEqual(snapshot.movement_count, 1)


class FundingTests(unittest.TestCase):
    def setUp(self):
        self.desk = FundingDesk()
        maturity = TODAY + timedelta(days=30)
        self.desk.quote(FundingQuote("bank-a", "USD", TODAY, maturity, "0.05", "0.04", "1000000"))
        self.desk.quote(FundingQuote("bank-b", "USD", TODAY, maturity, "0.045", "0.035", "500000"))

    def test_best_borrow_and_lend_quotes(self):
        maturity = TODAY + timedelta(days=30)
        borrow = FundingInstrument("loan", "USD", TODAY, maturity, "400000", FundingSide.BORROW)
        lend = FundingInstrument("deposit", "USD", TODAY, maturity, "400000", FundingSide.LEND)
        self.assertEqual(self.desk.best(borrow).provider, "bank-b")
        self.assertEqual(self.desk.best(lend).provider, "bank-a")
        self.assertGreater(self.desk.best(borrow).interest, 0)
        self.assertLess(self.desk.best(lend).interest, 0)

    def test_blended_funding_uses_capacity(self):
        instrument = FundingInstrument(
            "loan", "USD", TODAY, TODAY + timedelta(days=30), "1200000", FundingSide.BORROW,
        )
        decisions = self.desk.blended(instrument)
        self.assertEqual(sum((item.principal for item in decisions)), Decimal("1200000"))
        self.assertEqual(tuple(item.provider for item in decisions), ("bank-b", "bank-a"))


class CollateralTests(unittest.TestCase):
    def setUp(self):
        self.pool = CollateralPool("USD")
        self.pool.add(CollateralAsset(
            "cash", CollateralType.CASH, "USD", "1000", "0", "central-bank",
        ))
        self.pool.add(CollateralAsset(
            "bond", CollateralType.GOVERNMENT_BOND, "EUR", "1000", "0.1", "sovereign",
        ))

    def test_eligible_value_pledging_and_concentration(self):
        self.assertEqual(self.pool.eligible_total({"EURUSD": "1.1"}), Decimal("1990"))
        self.pool.pledge("bond", "counterparty")
        self.assertEqual(tuple(item.asset_id for item in self.pool.pledged("counterparty")), ("bond",))
        self.pool.release("bond", "counterparty")
        self.assertEqual(len(self.pool.available()), 2)
        self.assertEqual(sum(self.pool.issuer_concentration().values()), Decimal("1"))

    def test_margin_call_tracks_partial_and_complete_satisfaction(self):
        call = MarginCall("call", "counterparty", "USD", "100", NOW + timedelta(hours=2))
        self.pool.open_call(call)
        partial = self.pool.satisfy("call", "40")
        self.assertEqual(partial.state, MarginCallState.PARTIALLY_SATISFIED)
        complete = self.pool.satisfy("call", "60")
        self.assertEqual(complete.state, MarginCallState.SATISFIED)
        self.assertEqual(complete.outstanding, 0)


class NettingTests(unittest.TestCase):
    def obligation(self, identifier, payer, receiver, amount):
        return PaymentObligation(identifier, payer, receiver, "USD", Decimal(amount), TODAY, "master")

    def test_bilateral_netting_reduces_opposing_payments(self):
        result = NettingEngine().net((
            self.obligation("a", "bank-a", "bank-b", "100"),
            self.obligation("b", "bank-b", "bank-a", "40"),
        ))
        self.assertEqual(result.gross_amount, Decimal("140"))
        self.assertEqual(result.net_amount, Decimal("60"))
        self.assertEqual(result.instructions[0].payer, "bank-a")
        self.assertEqual(result.efficiency, Decimal("80") / Decimal("140"))

    def test_multilateral_netting_settles_party_balances(self):
        result = NettingEngine().multilateral((
            self.obligation("a", "a", "b", "100"),
            self.obligation("b", "b", "c", "70"),
            self.obligation("c", "c", "a", "20"),
        ))
        self.assertEqual(result.net_amount, Decimal("80"))
        self.assertLess(result.net_amount, result.gross_amount)


class SettlementTests(unittest.TestCase):
    def instruction(self):
        return SettlementInstruction(
            "settle", "trade", TODAY + timedelta(days=2), "counterparty",
            SettlementLeg("USD", "110", "ours-usd", "theirs-usd"),
            SettlementLeg("EUR", "100", "theirs-eur", "ours-eur"),
        )

    def test_matching_release_partial_and_complete_settlement(self):
        book = SettlementBook()
        book.add(self.instruction())
        matched = book.transition("settle", SettlementState.MATCHED, NOW, "matched")
        released = book.transition("settle", SettlementState.RELEASED, NOW + timedelta(minutes=1), "released")
        partial = book.settle_leg("settle", pay_amount="110", at=NOW + timedelta(minutes=2))
        self.assertEqual(partial.state, SettlementState.PARTIALLY_SETTLED)
        completed = book.settle_leg("settle", receive_amount="100", at=NOW + timedelta(minutes=3))
        self.assertEqual(completed.state, SettlementState.SETTLED)
        self.assertTrue(completed.complete)
        self.assertEqual(len(book.events("settle")), 4)

    def test_exposure_reports_outstanding_legs(self):
        book = SettlementBook()
        book.add(self.instruction())
        self.assertEqual(book.exposure("counterparty"), {"USD": Decimal("-110"), "EUR": Decimal("100")})


class LiquidityTests(unittest.TestCase):
    def setUp(self):
        self.planner = LiquidityPlanner()
        self.planner.add(ForecastCashflow(
            "in", "USD", TODAY + timedelta(days=1), Decimal("100"),
            CashflowConfidence.EXPECTED, "receivable", "trade",
        ))
        self.planner.add(ForecastCashflow(
            "out", "USD", TODAY + timedelta(days=2), Decimal("-300"),
            CashflowConfidence.CONTRACTUAL, "payable", "trade",
        ))

    def test_forecast_carries_balances_and_finds_shortfall(self):
        forecast = self.planner.forecast(TODAY, {"USD": "250"}, {"USD": "100"}, horizon_days=3)
        self.assertEqual(len(forecast.buckets), 3)
        self.assertEqual(forecast.buckets[0].closing_balance, Decimal("350"))
        self.assertEqual(forecast.maximum_shortfalls["USD"], Decimal("50"))
        self.assertEqual(forecast.first_shortfall_dates["USD"], TODAY + timedelta(days=2))

    def test_stress_reduces_inflows_and_increases_outflows(self):
        forecast = self.planner.forecast(TODAY, {"USD": "250"}, {"USD": "100"}, horizon_days=3)
        stressed = self.planner.stress(
            forecast, inflow_haircut="0.5", outflow_multiplier="1.2", buffer_multiplier="1.1",
        )
        self.assertLess(stressed.buckets[-1].closing_balance, forecast.buckets[-1].closing_balance)
        self.assertGreater(stressed.maximum_shortfalls["USD"], forecast.maximum_shortfalls["USD"])


if __name__ == "__main__":
    unittest.main()
