from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from forex_monitor.errors import StorageConflictError, ValidationError
from forex_monitor.marketmaking.analytics import DealerAnalytics
from forex_monitor.marketmaking.hedging import HedgeExecution, HedgePlanner, HedgePolicy
from forex_monitor.marketmaking.inventory import InventoryLimit, InventoryManager, InventoryTransaction
from forex_monitor.marketmaking.models import BookLevel, BookSide, DealerFill, HedgeState, QuoteState
from forex_monitor.marketmaking.orderbook import ConsolidatedOrderBook, VenueBookUpdate
from forex_monitor.marketmaking.quoting import PricingContext, QuoteEngine, QuotePolicy


NOW = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)


def level(venue: str, side: BookSide, price: str, quantity: str, sequence: int = 1) -> BookLevel:
    return BookLevel(venue, "EURUSD", side, Decimal(price), Decimal(quantity), NOW, sequence)


def market_book() -> ConsolidatedOrderBook:
    book = ConsolidatedOrderBook(stale_after=timedelta(seconds=5))
    book.apply(
        VenueBookUpdate(
            "one",
            "EBS",
            "EURUSD",
            1,
            NOW,
            (level("EBS", BookSide.BID, "1.0998", "500000"),),
            (level("EBS", BookSide.ASK, "1.1002", "400000"),),
            snapshot=True,
        )
    )
    return book


def quote_engine() -> QuoteEngine:
    engine = QuoteEngine()
    engine.add_policy(
        QuotePolicy(
            "eurusd",
            "EURUSD",
            Decimal("2"),
            Decimal("1"),
            Decimal("10"),
            ((Decimal("100000"), Decimal("0.5")),),
            inventory_skew_pips=Decimal("1"),
            client_markups=(("client-a", Decimal("0.25")),),
        )
    )
    return engine


def dealer_fill(fill_id: str = "fill-1", side: str = "buy", quantity: str = "100000") -> DealerFill:
    return DealerFill(fill_id, "quote-1", "EURUSD", side, Decimal(quantity), Decimal("1.10"), NOW, "client-a")


def test_order_book_consolidates_venues_and_sweeps_best_prices() -> None:
    book = market_book()
    book.apply(
        VenueBookUpdate(
            "two",
            "REUTERS",
            "EURUSD",
            1,
            NOW,
            (level("REUTERS", BookSide.BID, "1.0999", "200000"),),
            (level("REUTERS", BookSide.ASK, "1.1001", "150000"),),
            snapshot=True,
        )
    )
    snapshot = book.snapshot("EUR/USD", at=NOW + timedelta(seconds=1))
    assert snapshot.best_bid == Decimal("1.0999")
    assert snapshot.best_ask == Decimal("1.1001")
    result = book.sweep("EURUSD", "buy", "300000", at=NOW + timedelta(seconds=1))
    assert result.complete
    assert result.filled_quantity == Decimal("300000")
    assert result.levels[0][0] == "REUTERS"


def test_order_book_detects_sequence_gaps_without_mutation() -> None:
    book = market_book()
    update = VenueBookUpdate(
        "gap",
        "EBS",
        "EURUSD",
        3,
        NOW,
        (level("EBS", BookSide.BID, "1.0997", "100000", 3),),
        (),
    )
    with pytest.raises(StorageConflictError):
        book.apply(update)
    assert book.venue_sequences("EURUSD") == {"EBS": 1}
    assert book.gaps()[0].expected_sequence == 2


def test_zero_quantity_removes_a_price_level() -> None:
    book = market_book()
    book.apply(
        VenueBookUpdate(
            "delete",
            "EBS",
            "EURUSD",
            2,
            NOW,
            (level("EBS", BookSide.BID, "1.0998", "0", 2),),
            (),
        )
    )
    assert book.snapshot("EURUSD", at=NOW).best_bid is None


def test_stale_venues_are_excluded_and_reported() -> None:
    snapshot = market_book().snapshot("EURUSD", at=NOW + timedelta(seconds=6))
    assert not snapshot.bids
    assert snapshot.stale_venues == ("EBS",)


def test_quote_pricing_replays_request_and_records_partial_fill() -> None:
    engine = quote_engine()
    market = market_book().snapshot("EURUSD", at=NOW)
    quote = engine.price(
        quote_id="quote-1",
        request_id="request-1",
        client_id="client-a",
        symbol="EURUSD",
        side="buy",
        quantity="100000",
        market=market,
        context=PricingContext(volatility_pips=Decimal("0.5"), inventory_ratio=Decimal("0.5")),
        created_at=NOW,
    )
    replay = engine.price(
        quote_id="quote-2",
        request_id="request-1",
        client_id="client-a",
        symbol="EURUSD",
        side="buy",
        quantity="100000",
        market=market,
        context=PricingContext(),
        created_at=NOW + timedelta(seconds=1),
    )
    assert replay == quote
    fill = engine.fill("quote-1", fill_id="fill-a", quantity="40000", filled_at=NOW)
    assert fill.price == quote.price
    assert engine.quote("quote-1").filled_quantity == Decimal("40000")
    assert engine.quote("quote-1").state == QuoteState.LIVE


def test_expired_quote_cannot_be_filled() -> None:
    engine = quote_engine()
    quote = engine.price(
        quote_id="quote-1",
        request_id="request-1",
        client_id="client-a",
        symbol="EURUSD",
        side="sell",
        quantity="10000",
        market=market_book().snapshot("EURUSD", at=NOW),
        context=PricingContext(),
        created_at=NOW,
    )
    with pytest.raises(ValidationError):
        engine.fill(quote.quote_id, fill_id="late", quantity="1", filled_at=quote.expires_at + timedelta(seconds=1))
    assert engine.quote(quote.quote_id).state == QuoteState.EXPIRED


def test_inventory_posts_both_currency_legs_and_is_idempotent() -> None:
    manager = InventoryManager()
    manager.set_limit(InventoryLimit("EUR", Decimal("150000"), Decimal("250000")))
    manager.set_limit(InventoryLimit("USD", Decimal("150000"), Decimal("250000")))
    first = manager.apply_fill(dealer_fill())
    second = manager.apply_fill(dealer_fill())
    assert second == first
    assert manager.position("EUR").quantity == Decimal("-100000")
    assert manager.position("USD").quantity == Decimal("110000.00")
    assert len(manager.transactions()) == 2


def test_inventory_average_cost_and_realized_pnl() -> None:
    manager = InventoryManager()
    manager.apply_adjustment(InventoryTransaction("a", "EUR", Decimal("100"), Decimal("1.10"), "seed", NOW))
    manager.apply_adjustment(InventoryTransaction("b", "EUR", Decimal("100"), Decimal("1.20"), "seed", NOW))
    close = manager.apply_adjustment(InventoryTransaction("c", "EUR", Decimal("-50"), Decimal("1.25"), "sale", NOW))
    assert manager.position("EUR").average_cost == Decimal("1.15")
    assert close.realized_pnl == Decimal("5.00")


def test_inventory_limit_rejection_is_atomic() -> None:
    manager = InventoryManager()
    manager.set_limit(InventoryLimit("EUR", Decimal("10"), Decimal("20")))
    with pytest.raises(ValidationError):
        manager.apply_adjustment(InventoryTransaction("too-large", "EUR", Decimal("21"), Decimal("1"), "test", NOW))
    assert not manager.positions(include_flat=True)
    assert not manager.transactions()


def test_valuation_and_soft_limit_reporting() -> None:
    manager = InventoryManager()
    manager.set_limit(InventoryLimit("EUR", Decimal("50"), Decimal("100")))
    manager.apply_adjustment(InventoryTransaction("a", "EUR", Decimal("75"), Decimal("1.10"), "seed", NOW))
    breaches = manager.limit_breaches(at=NOW)
    assert breaches[0].severity == "soft"
    value = manager.valuations({"EUR": "1.20"})[0]
    assert value.unrealized_pnl == Decimal("7.50")


def test_hedge_planner_slices_routes_and_fills_inventory_hedge() -> None:
    planner = HedgePlanner(HedgePolicy("default", "USD", maximum_order_size=Decimal("30000"), minimum_order_size=Decimal("1000")))
    positions = (InventoryTransaction("a", "EUR", Decimal("90000"), Decimal("1.1"), "seed", NOW),)
    manager = InventoryManager()
    manager.apply_adjustment(positions[0])
    limits = {"EUR": InventoryLimit("EUR", Decimal("50000"), Decimal("100000"))}
    orders = planner.plan(plan_id="plan-1", positions=manager.positions(), limits=limits, created_at=NOW)
    assert len(orders) == 3
    routed = planner.route(orders[0].hedge_id, "EBS", expected_version=1)
    part = planner.record_execution(HedgeExecution("execution-1", routed.hedge_id, Decimal("10000"), Decimal("1.1"), "EBS", NOW))
    assert part.state == HedgeState.PARTIALLY_FILLED
    filled = planner.record_execution(HedgeExecution("execution-2", routed.hedge_id, Decimal("20000"), Decimal("1.2"), "EBS", NOW))
    assert filled.state == HedgeState.FILLED
    assert filled.average_fill_price == Decimal("1.166666666666666666666666667")


def test_hedge_plan_replay_returns_original_orders() -> None:
    planner = HedgePlanner(HedgePolicy("default", "USD"))
    position = InventoryManager()
    position.apply_adjustment(InventoryTransaction("a", "EUR", Decimal("90000"), Decimal("1"), "seed", NOW))
    limits = {"EUR": InventoryLimit("EUR", Decimal("50"), Decimal("100000"))}
    first = planner.plan(plan_id="same", positions=position.positions(), limits=limits, created_at=NOW)
    second = planner.plan(plan_id="same", positions=(), limits={}, created_at=NOW + timedelta(days=1))
    assert second == first


def test_dealer_analytics_calculates_markout_spread_and_total_pnl() -> None:
    fill = dealer_fill()
    markout = DealerAnalytics.markout(fill, market_price="1.09", horizon_seconds=60)
    capture = DealerAnalytics.spread_capture((fill,), {fill.fill_id: "1.095"})[0]
    manager = InventoryManager()
    manager.apply_adjustment(InventoryTransaction("a", "EUR", Decimal("100"), Decimal("1.0"), "seed", NOW))
    valuation = manager.valuations({"EUR": "1.1"})
    summary = DealerAnalytics.pnl_summary(captures=(capture,), markouts=(markout,), inventory=valuation)
    assert markout.pnl == Decimal("1000.00")
    assert capture.average_pips == Decimal("50")
    assert summary.total == capture.gross_pnl + markout.pnl + Decimal("10.0")


def test_client_quote_statistics_group_by_client() -> None:
    engine = quote_engine()
    quote = engine.price(
        quote_id="quote-1",
        request_id="request-1",
        client_id="client-a",
        symbol="EURUSD",
        side="buy",
        quantity="10000",
        market=market_book().snapshot("EURUSD", at=NOW),
        context=PricingContext(),
        created_at=NOW,
    )
    engine.fill(quote.quote_id, fill_id="filled", quantity="10000", filled_at=NOW)
    stats = DealerAnalytics.client_statistics((engine.quote(quote.quote_id),))[0]
    assert stats.requested == 1
    assert stats.filled == 1
    assert stats.hit_ratio == Decimal("1")
