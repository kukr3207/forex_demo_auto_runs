"""Venue, symbol, and trader execution scorecards."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Dict, Iterable, Mapping, Sequence, Tuple

from forex_monitor.errors import ValidationError
from forex_monitor.tca.analysis import ExecutionAnalysis, percentile


@dataclass(frozen=True)
class ExecutionScorecard:
    dimension: str
    value: str
    order_count: int
    ordered_quantity: Decimal
    executed_quantity: Decimal
    completion_rate: Decimal
    average_shortfall_bps: Decimal
    median_shortfall_bps: Decimal
    p90_shortfall_bps: Decimal
    total_execution_cost: Decimal
    total_opportunity_cost: Decimal
    total_commissions: Decimal


class ScorecardBuilder:
    @staticmethod
    def build(
        analyses: Iterable[ExecutionAnalysis],
        dimension: str,
        classifier: Callable[[ExecutionAnalysis], str],
    ) -> Tuple[ExecutionScorecard, ...]:
        normalized_dimension = dimension.strip()
        if not normalized_dimension:
            raise ValidationError("scorecard dimension cannot be blank")
        groups: Dict[str, list[ExecutionAnalysis]] = {}
        for analysis in analyses:
            value = classifier(analysis).strip()
            if not value:
                raise ValidationError("scorecard classifier returned a blank value")
            groups.setdefault(value, []).append(analysis)
        return tuple(
            ScorecardBuilder._summarize(normalized_dimension, value, items)
            for value, items in sorted(groups.items())
        )

    @staticmethod
    def by_symbol(analyses: Iterable[ExecutionAnalysis]) -> Tuple[ExecutionScorecard, ...]:
        return ScorecardBuilder.build(analyses, "symbol", lambda item: item.symbol)

    @staticmethod
    def by_side(analyses: Iterable[ExecutionAnalysis]) -> Tuple[ExecutionScorecard, ...]:
        return ScorecardBuilder.build(analyses, "side", lambda item: item.side.value)

    @staticmethod
    def _summarize(
        dimension: str,
        value: str,
        analyses: Sequence[ExecutionAnalysis],
    ) -> ExecutionScorecard:
        ordered = sum((item.ordered_quantity for item in analyses), Decimal("0"))
        executed = sum((item.executed_quantity for item in analyses), Decimal("0"))
        shortfalls = tuple(item.implementation_shortfall_bps for item in analyses)
        weighted_shortfall = sum(
            (item.implementation_shortfall_bps * item.ordered_quantity for item in analyses),
            Decimal("0"),
        )
        return ExecutionScorecard(
            dimension,
            value,
            len(analyses),
            ordered,
            executed,
            executed / ordered if ordered else Decimal("0"),
            weighted_shortfall / ordered if ordered else Decimal("0"),
            percentile(shortfalls, Decimal("0.5")),
            percentile(shortfalls, Decimal("0.9")),
            sum((item.execution_cost for item in analyses), Decimal("0")),
            sum((item.opportunity_cost for item in analyses), Decimal("0")),
            sum((item.commissions for item in analyses), Decimal("0")),
        )


def scorecard_as_dict(scorecard: ExecutionScorecard) -> Mapping[str, object]:
    return {
        "dimension": scorecard.dimension,
        "value": scorecard.value,
        "orderCount": scorecard.order_count,
        "orderedQuantity": str(scorecard.ordered_quantity),
        "executedQuantity": str(scorecard.executed_quantity),
        "completionRate": str(scorecard.completion_rate),
        "averageShortfallBps": str(scorecard.average_shortfall_bps),
        "medianShortfallBps": str(scorecard.median_shortfall_bps),
        "p90ShortfallBps": str(scorecard.p90_shortfall_bps),
        "executionCost": str(scorecard.total_execution_cost),
        "opportunityCost": str(scorecard.total_opportunity_cost),
        "commissions": str(scorecard.total_commissions),
    }

