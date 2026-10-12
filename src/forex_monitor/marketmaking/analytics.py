"""Dealer performance measurements built from quotes, fills, and marks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Dict, Iterable, Mapping, Optional, Tuple

from forex_monitor.errors import ValidationError
from forex_monitor.marketmaking.inventory import InventoryValuation
from forex_monitor.marketmaking.models import DealerFill, DealerQuote, QuoteState, number, pair, timestamp


@dataclass(frozen=True)
class Markout:
    fill_id: str
    symbol: str
    horizon_seconds: int
    fill_price: Decimal
    market_price: Decimal
    quantity: Decimal
    pnl: Decimal
    pips: Decimal


@dataclass(frozen=True)
class ClientQuoteStatistics:
    client_id: str
    requested: int
    filled: int
    expired: int
    cancelled: int
    requested_notional: Decimal
    filled_notional: Decimal

    @property
    def hit_ratio(self) -> Decimal:
        return Decimal(self.filled) / Decimal(self.requested) if self.requested else Decimal("0")


@dataclass(frozen=True)
class SpreadCapture:
    symbol: str
    fills: int
    quantity: Decimal
    gross_pnl: Decimal
    average_pips: Decimal


@dataclass(frozen=True)
class DealerPnlSummary:
    spread_pnl: Decimal
    markout_pnl: Decimal
    inventory_unrealized: Decimal
    inventory_realized: Decimal
    funding_pnl: Decimal

    @property
    def total(self) -> Decimal:
        return self.spread_pnl + self.markout_pnl + self.inventory_unrealized + self.inventory_realized + self.funding_pnl


class DealerAnalytics:
    @staticmethod
    def markout(fill: DealerFill, *, market_price: object, horizon_seconds: int) -> Markout:
        if horizon_seconds < 0:
            raise ValidationError("markout horizon cannot be negative")
        market = number(market_price, "market price", positive=True)
        direction = Decimal("-1") if fill.side == "buy" else Decimal("1")
        pnl = (market - fill.price) * fill.quantity * direction
        pip_size = Decimal("0.01") if fill.symbol.endswith("JPY") else Decimal("0.0001")
        pips = (market - fill.price) * direction / pip_size
        return Markout(fill.fill_id, fill.symbol, horizon_seconds, fill.price, market, fill.quantity, pnl, pips)

    @staticmethod
    def markouts(
        fills: Iterable[DealerFill],
        marks: Mapping[Tuple[str, int], object],
        horizons: Iterable[int],
    ) -> Tuple[Markout, ...]:
        result = []
        requested_horizons = tuple(sorted(set(horizons)))
        if any(item < 0 for item in requested_horizons):
            raise ValidationError("markout horizons cannot be negative")
        for fill in fills:
            for horizon in requested_horizons:
                key = (fill.fill_id, horizon)
                if key not in marks:
                    raise ValidationError("missing markout observation for %s" % fill.fill_id)
                result.append(DealerAnalytics.markout(fill, market_price=marks[key], horizon_seconds=horizon))
        return tuple(result)

    @staticmethod
    def client_statistics(quotes: Iterable[DealerQuote]) -> Tuple[ClientQuoteStatistics, ...]:
        totals: Dict[str, Dict[str, object]] = {}
        for quote in quotes:
            row = totals.setdefault(
                quote.client_id,
                {
                    "requested": 0,
                    "filled": 0,
                    "expired": 0,
                    "cancelled": 0,
                    "requested_notional": Decimal("0"),
                    "filled_notional": Decimal("0"),
                },
            )
            row["requested"] = int(row["requested"]) + 1
            row["requested_notional"] = Decimal(row["requested_notional"]) + quote.quantity * quote.price
            if quote.filled_quantity:
                row["filled"] = int(row["filled"]) + 1
                row["filled_notional"] = Decimal(row["filled_notional"]) + quote.filled_quantity * quote.price
            if quote.state == QuoteState.EXPIRED:
                row["expired"] = int(row["expired"]) + 1
            if quote.state == QuoteState.CANCELLED:
                row["cancelled"] = int(row["cancelled"]) + 1
        return tuple(
            ClientQuoteStatistics(client_id=client, **values)  # type: ignore[arg-type]
            for client, values in sorted(totals.items())
        )

    @staticmethod
    def spread_capture(fills: Iterable[DealerFill], reference_prices: Mapping[str, object]) -> Tuple[SpreadCapture, ...]:
        totals: Dict[str, Tuple[int, Decimal, Decimal, Decimal]] = {}
        for fill in fills:
            if fill.fill_id not in reference_prices:
                raise ValidationError("missing reference price for %s" % fill.fill_id)
            reference = number(reference_prices[fill.fill_id], "reference price", positive=True)
            direction = Decimal("1") if fill.side == "buy" else Decimal("-1")
            pnl = (fill.price - reference) * fill.quantity * direction
            pip_size = Decimal("0.01") if fill.symbol.endswith("JPY") else Decimal("0.0001")
            pips = (fill.price - reference) * direction / pip_size
            count, quantity, gross, pip_total = totals.get(fill.symbol, (0, Decimal("0"), Decimal("0"), Decimal("0")))
            totals[fill.symbol] = count + 1, quantity + fill.quantity, gross + pnl, pip_total + pips
        return tuple(
            SpreadCapture(symbol, count, quantity, gross, pip_total / count)
            for symbol, (count, quantity, gross, pip_total) in sorted(totals.items())
        )

    @staticmethod
    def pnl_summary(
        *,
        captures: Iterable[SpreadCapture],
        markouts: Iterable[Markout],
        inventory: Iterable[InventoryValuation],
    ) -> DealerPnlSummary:
        spread = sum((item.gross_pnl for item in captures), Decimal("0"))
        markout = sum((item.pnl for item in markouts), Decimal("0"))
        unrealized = sum((item.unrealized_pnl for item in inventory), Decimal("0"))
        realized = sum((item.realized_pnl for item in inventory), Decimal("0"))
        funding = sum((item.funding_pnl for item in inventory), Decimal("0"))
        return DealerPnlSummary(spread, markout, unrealized, realized, funding)
