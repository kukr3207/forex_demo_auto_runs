"""Deterministic price-shock scenarios for open positions."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping, Sequence, Tuple

from forex_monitor.models import as_decimal, normalize_symbol
from forex_monitor.portfolio.position import Position


@dataclass(frozen=True)
class ScenarioResult:
    name: str
    pnl: Decimal
    shocked_prices: Mapping[str, Decimal]


def apply_scenario(
    name: str, positions: Sequence[Position], shocks: Mapping[str, object]
) -> ScenarioResult:
    if not name.strip():
        raise ValueError("scenario name cannot be blank")
    parsed = {
        normalize_symbol(symbol): as_decimal(shock, "scenario shock")
        for symbol, shock in shocks.items()
    }
    prices = {}
    pnl = Decimal("0")
    for position in positions:
        shock = parsed.get(position.symbol, Decimal("0"))
        if shock <= Decimal("-1"):
            raise ValueError("scenario shocks must remain above negative one")
        price = position.current_price * (Decimal("1") + shock)
        prices[position.symbol] = price
        pnl += position.mark(price).unrealized_pnl - position.unrealized_pnl
    return ScenarioResult(name.strip(), pnl, prices)


def scenario_matrix(
    positions: Sequence[Position], scenarios: Mapping[str, Mapping[str, object]]
) -> Tuple[ScenarioResult, ...]:
    return tuple(apply_scenario(name, positions, scenarios[name]) for name in sorted(scenarios))
