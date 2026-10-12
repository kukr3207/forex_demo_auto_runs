from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from forex_monitor.errors import StorageConflictError, ValidationError
from forex_monitor.execution.algorithms import AlgorithmType, ParticipationScheduler
from forex_monitor.execution.allocations import AllocationBook, AllocationRequest, allocate_fill
from forex_monitor.execution.fills import Fill, FillLedger
from forex_monitor.execution.orders import (
    Order, OrderBook, OrderSide, OrderState, OrderType, TimeInForce,
)
from forex_monitor.execution.reconciliation import (
    ExecutionReconciler, ExternalExecution, IssueKind,
)
from forex_monitor.execution.routing import SmartOrderRouter
from forex_monitor.execution.venues import Venue, VenueBook, VenueQuote, VenueState


NOW = datetime(2026, 1, 5, 12, tzinfo=timezone.utc)


def order(**changes):
    values = dict(
        order_id="ord-1", symbol="EURUSD", side=OrderSide.BUY,
        quantity=Decimal("1000000"), state=OrderState.APPROVED,
        created_at=NOW, updated_at=NOW,
    )
    values.update(changes)
    return Order(**values)


class OrderTests(unittest.TestCase):
    def test_lifecycle_and_weighted_average_fill(self):
        routed = order().transition(OrderState.ROUTED, NOW + timedelta(seconds=1))
        partial = routed.apply_fill("250000", "1.10", NOW + timedelta(seconds=2))
        complete = partial.apply_fill("750000", "1.12", NOW + timedelta(seconds=3))
        self.assertEqual(partial.state, OrderState.PARTIALLY_FILLED)
        self.assertEqual(complete.state, OrderState.FILLED)
        self.assertEqual(complete.average_price, Decimal("1.115"))
        self.assertEqual(complete.remaining_quantity, 0)
        self.assertEqual(complete.version, 4)

    def test_invalid_order_shapes_and_transitions(self):
        with self.assertRaises(ValidationError):
            order(order_type=OrderType.LIMIT)
        with self.assertRaises(ValidationError):
            order(time_in_force=TimeInForce.GOOD_TILL_TIME)
        with self.assertRaises(ValidationError):
            order().transition(OrderState.FILLED, NOW)

    def test_order_book_idempotency_versions_and_expiry(self):
        book = OrderBook()
        original = order(order_id="exp", expires_at=NOW + timedelta(minutes=1), time_in_force=TimeInForce.GOOD_TILL_TIME)
        self.assertIs(book.add(original), book.add(original))
        with self.assertRaises(StorageConflictError):
            book.add(order(order_id="exp", quantity="2"))
        expired = book.expire(NOW + timedelta(minutes=2))
        self.assertEqual(expired[0].state, OrderState.EXPIRED)
        self.assertEqual(book.active(), ())


class VenueAndRoutingTests(unittest.TestCase):
    def setUp(self):
        self.book = VenueBook()
        self.book.register(Venue("a", "Alpha", priority=2, taker_fee_bps=Decimal("0.1"), supported_symbols=("EURUSD",)))
        self.book.register(Venue("b", "Beta", priority=1, taker_fee_bps=Decimal("0.2"), supported_symbols=("EURUSD",)))
        self.book.quote(VenueQuote("a", "EURUSD", "1.0998", "1.1002", "800000", "600000", NOW, "qa"))
        self.book.quote(VenueQuote("b", "EURUSD", "1.0999", "1.1001", "400000", "500000", NOW, "qb"))

    def test_router_splits_liquidity_by_effective_cost(self):
        router = SmartOrderRouter(self.book, maximum_quote_age=timedelta(seconds=2))
        decision = router.route(order(quantity="900000"), NOW + timedelta(seconds=1))
        self.assertTrue(decision.complete)
        self.assertEqual(decision.routed_quantity, Decimal("900000"))
        self.assertEqual(len(decision.legs), 2)
        self.assertEqual(sum((leg.quantity for leg in decision.legs)), Decimal("900000"))
        self.assertIsNotNone(decision.average_price)

    def test_limits_staleness_and_halts_remove_quotes(self):
        router = SmartOrderRouter(self.book, maximum_quote_age=timedelta(seconds=2))
        limited = order(quantity="100000", order_type=OrderType.LIMIT, limit_price=Decimal("1.10"))
        self.assertEqual(router.route(limited, NOW).routed_quantity, 0)
        self.assertEqual(router.route(order(quantity="100000"), NOW + timedelta(seconds=3)).routed_quantity, 0)
        self.book.update_state("a", VenueState.HALTED, NOW + timedelta(seconds=1))
        executable = self.book.executable("EURUSD", "100000", NOW + timedelta(seconds=1), timedelta(seconds=2))
        self.assertEqual(tuple(item[0].venue_id for item in executable), ("b",))


class AlgorithmTests(unittest.TestCase):
    def test_twap_preserves_exact_quantity(self):
        plan = ParticipationScheduler.twap(order(), NOW, NOW + timedelta(minutes=10), 3)
        self.assertEqual(plan.algorithm, AlgorithmType.TWAP)
        self.assertEqual(plan.quantity, Decimal("1000000"))
        self.assertEqual(len(plan.slices), 3)
        child = plan.child_order(order(), plan.slices[0])
        self.assertEqual(child.parent_order_id, "ord-1")

    def test_vwap_uses_volume_profile(self):
        plan = ParticipationScheduler.vwap(order(), NOW, timedelta(minutes=1), (1, 2, 1))
        self.assertEqual(tuple(item.quantity for item in plan.slices), (
            Decimal("250000"), Decimal("500000"), Decimal("250000"),
        ))

    def test_participation_caps_at_parent_remainder(self):
        plan = ParticipationScheduler.participation(
            order(quantity="100"),
            ((NOW, 200), (NOW + timedelta(minutes=1), 300)),
            Decimal("0.25"),
        )
        self.assertEqual(plan.quantity, Decimal("100"))
        self.assertEqual(len(plan.slices), 2)


class FillAndAllocationTests(unittest.TestCase):
    def fill(self, **changes):
        values = dict(
            fill_id="fill-1", order_id="ord-1", symbol="EURUSD", side=OrderSide.BUY,
            quantity=Decimal("100"), price=Decimal("1.1"), executed_at=NOW,
            venue_id="venue", commission=Decimal("3"), external_id="external-1",
        )
        values.update(changes)
        return Fill(**values)

    def test_fill_ledger_is_idempotent_and_summarizes(self):
        ledger = FillLedger()
        fill = self.fill()
        self.assertIs(ledger.book(fill), ledger.book(fill))
        ledger.book(self.fill(fill_id="fill-2", external_id="external-2", side=OrderSide.SELL, quantity="40", price="1.2"))
        summary = ledger.summary("EURUSD")
        self.assertEqual(summary.quantity, Decimal("60"))
        self.assertEqual(summary.fill_count, 2)
        with self.assertRaises(StorageConflictError):
            ledger.book(self.fill(price="1.2"))

    def test_allocations_preserve_fill_quantity_and_commission(self):
        allocations = allocate_fill(
            self.fill(quantity="100.01"),
            (
                AllocationRequest("alpha", Decimal("2")),
                AllocationRequest("beta", Decimal("1")),
            ),
        )
        self.assertEqual(sum((item.quantity for item in allocations)), Decimal("100.01"))
        self.assertEqual(sum((item.commission for item in allocations)), Decimal("3"))
        book = AllocationBook()
        self.assertEqual(book.book(allocations), book.book(allocations))
        self.assertEqual(len(book.for_account("alpha")), 1)


class ReconciliationTests(unittest.TestCase):
    def test_reconciliation_reports_missing_and_mismatched_fills(self):
        orders = OrderBook()
        routed = order(quantity="100").transition(OrderState.ROUTED, NOW + timedelta(seconds=1))
        filled = routed.apply_fill("100", "1.1", NOW + timedelta(seconds=2))
        orders.add(filled)
        fills = FillLedger()
        fills.book(Fill("fill", "ord-1", "EURUSD", OrderSide.BUY, "100", "1.1", NOW, "venue", external_id="ext"))
        external = (
            ExternalExecution("ext", "ord-1", "venue", Decimal("90"), Decimal("1.2"), "partial"),
            ExternalExecution("unknown", "ord-x", "venue", Decimal("1"), Decimal("1.2"), "filled"),
        )
        issues = ExecutionReconciler(orders, fills).reconcile(external)
        kinds = {issue.kind for issue in issues}
        self.assertIn(IssueKind.QUANTITY_MISMATCH, kinds)
        self.assertIn(IssueKind.PRICE_MISMATCH, kinds)
        self.assertIn(IssueKind.STATE_MISMATCH, kinds)
        self.assertIn(IssueKind.UNKNOWN_ORDER, kinds)


if __name__ == "__main__":
    unittest.main()
