"""Target allocation normalization and rebalance calculations."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, Mapping, Tuple

from forex_monitor.models import as_decimal, normalize_symbol


@dataclass(frozen=True)
class RebalanceInstruction:
    symbol: str
    current_notional: Decimal
    target_notional: Decimal

    @property
    def difference(self) -> Decimal:
        return self.target_notional - self.current_notional


def normalize_weights(weights: Mapping[str, object]) -> Mapping[str, Decimal]:
    parsed: Dict[str, Decimal] = {}
    for symbol, value in weights.items():
        weight = as_decimal(value, "allocation weight")
        if weight < 0:
            raise ValueError("allocation weights cannot be negative")
        parsed[normalize_symbol(symbol)] = weight
    total = sum(parsed.values(), Decimal("0"))
    if total <= 0:
        raise ValueError("allocation weights must have a positive total")
    return {symbol: weight / total for symbol, weight in parsed.items()}


def rebalance_plan(
    current: Mapping[str, object], weights: Mapping[str, object], equity: object
) -> Tuple[RebalanceInstruction, ...]:
    capital = as_decimal(equity, "equity", positive=True)
    targets = normalize_weights(weights)
    symbols = sorted(set(current) | set(targets))
    return tuple(
        RebalanceInstruction(
            normalize_symbol(symbol),
            as_decimal(current.get(symbol, 0), "current notional"),
            targets.get(normalize_symbol(symbol), Decimal("0")) * capital,
        )
        for symbol in symbols
    )
