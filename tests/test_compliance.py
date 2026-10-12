from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import unittest

from forex_monitor.compliance.approvals import (
    ApprovalDecision, ApprovalPolicy, ApprovalState, ApprovalWorkflow,
)
from forex_monitor.compliance.limits import (
    ComplianceLimit, LimitAction, LimitEngine, LimitMetric,
)
from forex_monitor.compliance.reporting import RegulatoryReporter
from forex_monitor.compliance.screening import (
    PartyProfile, RiskRating, ScreeningDisposition, ScreeningEngine, WatchlistEntry,
    normalize_name, token_similarity,
)
from forex_monitor.compliance.surveillance import AlertType, SurveillanceEngine
from forex_monitor.errors import StorageConflictError, ValidationError
from forex_monitor.execution.fills import Fill
from forex_monitor.execution.orders import Order, OrderSide, OrderState


NOW = datetime(2026, 3, 2, 10, tzinfo=timezone.utc)


class ScreeningTests(unittest.TestCase):
    def test_name_normalization_and_similarity(self):
        self.assertEqual(normalize_name("Société-Générale S.A."), "SOCIETE GENERALE S A")
        self.assertEqual(token_similarity("Alpha Trading Ltd", "Trading Alpha"), Decimal("2") / Decimal("3"))

    def test_block_review_and_clear_dispositions(self):
        engine = ScreeningEngine(review_threshold="0.4", block_threshold="0.75")
        engine.add(WatchlistEntry("w1", "Blocked Alpha Trading", "sanctions", aliases=("Alpha Trading",)))
        blocked = engine.screen(PartyProfile("p1", "Alpha Trading", "GB", RiskRating.LOW))
        self.assertEqual(blocked.disposition, ScreeningDisposition.BLOCK)
        review = engine.screen(PartyProfile("p2", "Unknown Person", "GB", RiskRating.HIGH))
        self.assertEqual(review.disposition, ScreeningDisposition.REVIEW)
        clear = engine.screen(PartyProfile("p3", "Ordinary Company", "US", RiskRating.LOW))
        self.assertTrue(clear.clear)

    def test_country_and_identifier_matches_block(self):
        engine = ScreeningEngine()
        engine.restrict_country("XY")
        engine.add(WatchlistEntry("w", "Different Name", "regulator", identifiers=("REG-1",)))
        result = engine.screen(PartyProfile("p", "Company", "XY", RiskRating.LOW, registration_id="REG-1"))
        self.assertEqual(result.disposition, ScreeningDisposition.BLOCK)
        self.assertTrue(result.identifier_match)
        self.assertTrue(result.country_restricted)


class LimitTests(unittest.TestCase):
    def test_hierarchical_limits_and_headroom(self):
        engine = LimitEngine()
        engine.add(ComplianceLimit("global", LimitMetric.ORDER_NOTIONAL, "1000000", LimitAction.WARN))
        engine.add(ComplianceLimit("desk", LimitMetric.ORDER_NOTIONAL, "500000", LimitAction.BLOCK, account_id="desk"))
        result = engine.evaluate({"account_id": "desk"}, {LimitMetric.ORDER_NOTIONAL: "600000"})
        self.assertTrue(result.blocked)
        self.assertFalse(result.allowed)
        self.assertEqual(result.breaches[0].limit_id, "desk")
        self.assertEqual(engine.headroom({"account_id": "desk"}, LimitMetric.ORDER_NOTIONAL, "400000"), Decimal("100000"))

    def test_unmatched_selectors_do_not_apply(self):
        engine = LimitEngine()
        engine.add(ComplianceLimit("strategy", LimitMetric.LOSS, "100", LimitAction.REQUIRE_APPROVAL, strategy_id="alpha"))
        self.assertEqual(engine.evaluate({"strategy_id": "beta"}, {"loss": "1000"}).breaches, ())


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.workflow = ApprovalWorkflow()
        self.workflow.add_policy(ApprovalPolicy(
            "two-person", minimum_approvals=2, required_roles=("risk", "trading"),
            expires_after=timedelta(hours=1),
        ))
        self.request = self.workflow.request("req", "order", "ord", "maker", "two-person", NOW)

    def test_required_roles_and_four_eyes(self):
        pending = self.workflow.decide("req", ApprovalDecision("risk-user", "risk", True, NOW + timedelta(minutes=1)))
        self.assertEqual(pending.state, ApprovalState.PENDING)
        approved = self.workflow.decide("req", ApprovalDecision("trader", "trading", True, NOW + timedelta(minutes=2)))
        self.assertEqual(approved.state, ApprovalState.APPROVED)

    def test_self_approval_and_repeated_decision_are_rejected(self):
        with self.assertRaises(ValidationError):
            self.workflow.decide("req", ApprovalDecision("maker", "risk", True, NOW))
        self.workflow.decide("req", ApprovalDecision("risk-user", "risk", True, NOW))
        with self.assertRaises(StorageConflictError):
            self.workflow.decide("req", ApprovalDecision("risk-user", "risk", True, NOW))

    def test_expiry_updates_pending_requests(self):
        expired = self.workflow.expire(NOW + timedelta(hours=2))
        self.assertEqual(expired[0].state, ApprovalState.EXPIRED)


def sample_order(identifier="order", side=OrderSide.BUY, state=OrderState.FILLED):
    return Order(
        identifier, "EURUSD", side, Decimal("100"), state=state,
        filled_quantity=Decimal("100") if state is OrderState.FILLED else Decimal("0"),
        average_price=Decimal("1.1") if state is OrderState.FILLED else None,
        account_id="desk", created_at=NOW, updated_at=NOW,
    )


def sample_fill(identifier="fill", order_id="order", side=OrderSide.BUY, price="1.1", seconds=0):
    return Fill(
        identifier, order_id, "EURUSD", side, Decimal("100"), Decimal(price),
        NOW + timedelta(seconds=seconds), "venue", external_id="ext-" + identifier,
    )


class SurveillanceTests(unittest.TestCase):
    def test_opposing_fills_raise_wash_and_reversal_alerts(self):
        engine = SurveillanceEngine(reversal_window=timedelta(minutes=5))
        alerts = engine.fills(
            (sample_fill("a"), sample_fill("b", "order-b", OrderSide.SELL, seconds=10)),
            {"order": "desk", "order-b": "desk"},
        )
        self.assertEqual(alerts[0].alert_type, AlertType.WASH_TRADE)
        self.assertEqual(alerts[0].account_id, "desk")

    def test_off_market_execution_uses_basis_point_threshold(self):
        engine = SurveillanceEngine(price_deviation_bps="10")
        self.assertIsNone(engine.off_market(sample_fill(price="1.1005"), "1.1", "desk"))
        alert = engine.off_market(sample_fill(price="1.102"), "1.1", "desk")
        self.assertEqual(alert.alert_type, AlertType.OFF_MARKET_EXECUTION)

    def test_cancellation_ratio_alert(self):
        values = [
            Order(
                "o%d" % index, "EURUSD", OrderSide.BUY, Decimal("1"),
                state=OrderState.CANCELLED if index < 4 else OrderState.APPROVED,
                account_id="desk", created_at=NOW, updated_at=NOW,
            )
            for index in range(5)
        ]
        alerts = SurveillanceEngine(cancellation_ratio="0.8").order_activity(values)
        self.assertIn(AlertType.EXCESSIVE_CANCELLATION, {item.alert_type for item in alerts})


class ReportingTests(unittest.TestCase):
    def test_fill_report_is_idempotent_and_exports_canonical_jsonl(self):
        reporter = RegulatoryReporter("LEI-REPORTER")
        order = sample_order()
        fill = sample_fill()
        report = reporter.from_fill("report", "mifir", order, fill, counterparty="LEI-CP")
        self.assertIs(reporter.store(report), reporter.store(report))
        payload = json.loads(reporter.export_jsonl())
        self.assertEqual(payload["reportId"], "report")
        self.assertEqual(payload["regime"], "MIFIR")
        self.assertEqual(len(report.digest), 64)

    def test_order_fill_mismatch_is_rejected(self):
        reporter = RegulatoryReporter("LEI")
        with self.assertRaises(ValidationError):
            reporter.from_fill("r", "mifir", sample_order(), sample_fill(order_id="other"))


if __name__ == "__main__":
    unittest.main()
