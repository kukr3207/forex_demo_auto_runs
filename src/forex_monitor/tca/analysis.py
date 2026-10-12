"""Fill-level slippage and parent-order implementation shortfall."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Optional, Sequence, Tuple

from forex_monitor.errors import ValidationError
from forex_monitor.execution.fills import Fill
from forex_monitor.execution.orders import OrderSide
from forex_monitor.models import as_decimal, normalize_symbol
from forex_monitor.tca.benchmarks import BenchmarkValue


@dataclass(frozen=True)
class FillCost:
    fill_id: str
    order_id: str
    symbol: str
    venue_id: str
    quantity: Decimal
    execution_price: Decimal
    benchmark_price: Decimal
    slippage: Decimal
    slippage_bps: Decimal
    cost_quote: Decimal
    commission: Decimal


@dataclass(frozen=True)
class ExecutionAnalysis:
    order_id: str
    symbol: str
    side: OrderSide
    ordered_quantity: Decimal
    executed_quantity: Decimal
    unexecuted_quantity: Decimal
    average_execution_price: Optional[Decimal]
    benchmark_price: Decimal
    implementation_shortfall: Decimal
    implementation_shortfall_bps: Decimal
    execution_cost: Decimal
    opportunity_cost: Decimal
    commissions: Decimal
    fill_costs: Tuple[FillCost, ...]

    @property
    def completion_rate(self) -> Decimal:
        if self.ordered_quantity == 0:
            return Decimal("0")
        return self.executed_quantity / self.ordered_quantity


class ExecutionAnalyzer:
    @staticmethod
    def analyze_fill(fill: Fill, benchmark: BenchmarkValue) -> FillCost:
        if fill.symbol != benchmark.symbol:
            raise ValidationError("fill and benchmark symbols differ")
        direction = Decimal("1") if fill.side is OrderSide.BUY else Decimal("-1")
        slippage = (fill.price - benchmark.price) * direction
        slippage_bps = slippage / benchmark.price * Decimal("10000")
        return FillCost(
            fill.fill_id,
            fill.order_id,
            fill.symbol,
            fill.venue_id,
            fill.quantity,
            fill.price,
            benchmark.price,
            slippage,
            slippage_bps,
            slippage * fill.quantity,
            fill.commission,
        )

    @classmethod
    def analyze_order(
        cls,
        order_id: str,
        symbol: str,
        side: OrderSide,
        ordered_quantity: object,
        fills: Sequence[Fill],
        benchmark: BenchmarkValue,
        decision_price: Optional[object] = None,
    ) -> ExecutionAnalysis:
        identifier = order_id.strip()
        if not identifier:
            raise ValidationError("order id cannot be blank")
        normalized = normalize_symbol(symbol)
        parsed_side = side if isinstance(side, OrderSide) else OrderSide(side)
        ordered = as_decimal(ordered_quantity, "ordered quantity", positive=True)
        if benchmark.symbol != normalized:
            raise ValidationError("order and benchmark symbols differ")
        ordered_fills = tuple(sorted(fills, key=lambda item: (item.executed_at, item.fill_id)))
        if any(fill.order_id != identifier for fill in ordered_fills):
            raise ValidationError("analysis contains a fill for another order")
        if any(fill.symbol != normalized or fill.side is not parsed_side for fill in ordered_fills):
            raise ValidationError("analysis fill does not match order symbol and side")
        executed = sum((fill.quantity for fill in ordered_fills), Decimal("0"))
        if executed > ordered:
            raise ValidationError("executed quantity exceeds ordered quantity")
        execution_notional = sum((fill.notional for fill in ordered_fills), Decimal("0"))
        average = execution_notional / executed if executed else None
        fill_costs = tuple(cls.analyze_fill(fill, benchmark) for fill in ordered_fills)
        execution_cost = sum((item.cost_quote for item in fill_costs), Decimal("0"))
        unexecuted = ordered - executed
        decision = benchmark.price if decision_price is None else as_decimal(decision_price, "decision price", positive=True)
        direction = Decimal("1") if parsed_side is OrderSide.BUY else Decimal("-1")
        opportunity_cost = (benchmark.price - decision) * direction * unexecuted
        total = execution_cost + opportunity_cost
        denominator = benchmark.price * ordered
        commissions = sum((fill.commission for fill in ordered_fills), Decimal("0"))
        return ExecutionAnalysis(
            identifier,
            normalized,
            parsed_side,
            ordered,
            executed,
            unexecuted,
            average,
            benchmark.price,
            total,
            total / denominator * Decimal("10000"),
            execution_cost,
            opportunity_cost,
            commissions,
            fill_costs,
        )


def percentile(values: Iterable[object], probability: object) -> Decimal:
    ordered = sorted(as_decimal(value, "percentile value") for value in values)
    if not ordered:
        raise ValidationError("percentile requires values")
    parsed = as_decimal(probability, "percentile probability")
    if parsed < 0 or parsed > 1:
        raise ValidationError("percentile probability must be between zero and one")
    if len(ordered) == 1:
        return ordered[0]
    position = parsed * Decimal(len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - Decimal(lower)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction

