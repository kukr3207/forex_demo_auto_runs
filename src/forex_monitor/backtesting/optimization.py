"""Deterministic parameter-grid generation and result ranking."""

from dataclasses import dataclass
from decimal import Decimal
from itertools import product
from typing import Mapping, Sequence, Tuple


@dataclass(frozen=True)
class ParameterScore:
    parameters: Mapping[str, object]
    score: Decimal


def parameter_grid(values: Mapping[str, Sequence[object]]) -> Tuple[Mapping[str, object], ...]:
    names = tuple(sorted(values))
    if not names or any(not values[name] for name in names):
        raise ValueError("each optimization parameter requires at least one value")
    return tuple(
        dict(zip(names, combination)) for combination in product(*(values[name] for name in names))
    )


def rank_scores(scores: Sequence[ParameterScore]) -> Tuple[ParameterScore, ...]:
    return tuple(
        sorted(
            scores,
            key=lambda item: (
                -item.score,
                tuple((name, str(value)) for name, value in sorted(item.parameters.items())),
            ),
        )
    )


def best_score(scores: Sequence[ParameterScore]) -> ParameterScore:
    ranked = rank_scores(scores)
    if not ranked:
        raise ValueError("at least one parameter score is required")
    return ranked[0]
