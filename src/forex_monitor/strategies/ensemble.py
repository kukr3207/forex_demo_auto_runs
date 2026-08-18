"""Confidence-weighted combination of strategy decisions."""

from decimal import Decimal
from typing import Sequence

from forex_monitor.models import Signal
from forex_monitor.strategies.base import StrategyDecision

_SCORES = {
    Signal.STRONG_BUY: Decimal("2"),
    Signal.BUY: Decimal("1"),
    Signal.NEUTRAL: Decimal("0"),
    Signal.SELL: Decimal("-1"),
    Signal.STRONG_SELL: Decimal("-2"),
}


def combine_decisions(decisions: Sequence[StrategyDecision]) -> StrategyDecision:
    if not decisions:
        raise ValueError("at least one strategy decision is required")
    total_weight = sum((decision.confidence for decision in decisions), Decimal("0"))
    if total_weight == 0:
        return StrategyDecision("ensemble", Signal.NEUTRAL, Decimal("0"), ("no_confidence",), {})
    score = (
        sum(
            (_SCORES[decision.signal] * decision.confidence for decision in decisions),
            Decimal("0"),
        )
        / total_weight
    )
    signal = (
        Signal.BUY
        if score >= Decimal("0.5")
        else Signal.SELL
        if score <= Decimal("-0.5")
        else Signal.NEUTRAL
    )
    confidence = min(Decimal("1"), abs(score) / Decimal("2"))
    return StrategyDecision(
        "ensemble",
        signal,
        confidence,
        tuple(decision.strategy for decision in decisions),
        {"score": score},
    )
