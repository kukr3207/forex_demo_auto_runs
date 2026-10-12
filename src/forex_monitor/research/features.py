"""Leakage-aware feature engineering over ordered market observations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Callable, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from forex_monitor.errors import ValidationError
from forex_monitor.models import as_decimal
from forex_monitor.research.datasets import Observation


FeatureFunction = Callable[[Sequence[Observation], int], Optional[Decimal]]


@dataclass(frozen=True)
class FeatureDefinition:
    name: str
    function: FeatureFunction
    lookback: int = 1
    dependencies: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValidationError("feature name cannot be blank")
        if self.lookback <= 0:
            raise ValidationError("feature lookback must be positive")


@dataclass(frozen=True)
class FeatureRow:
    observed_at: datetime
    symbol: str
    values: Mapping[str, Decimal]


class FeaturePipeline:
    def __init__(self) -> None:
        self._definitions: Dict[str, FeatureDefinition] = {}

    def add(self, definition: FeatureDefinition) -> None:
        if definition.name in self._definitions:
            raise ValidationError("feature name is already registered")
        missing = [name for name in definition.dependencies if name not in self._definitions]
        if missing:
            raise ValidationError("feature dependencies are not registered: " + ", ".join(missing))
        self._definitions[definition.name] = definition

    def transform(self, observations: Sequence[Observation]) -> Tuple[FeatureRow, ...]:
        ordered = tuple(sorted(observations, key=lambda item: (item.symbol, item.observed_at)))
        grouped: Dict[str, list[Observation]] = {}
        for item in ordered:
            grouped.setdefault(item.symbol, []).append(item)
        rows = []
        for symbol, values in grouped.items():
            for index, observation in enumerate(values):
                result: Dict[str, Decimal] = {}
                for name, definition in self._definitions.items():
                    if index + 1 < definition.lookback:
                        continue
                    value = definition.function(values, index)
                    if value is not None:
                        result[name] = as_decimal(value, name)
                rows.append(FeatureRow(observation.observed_at, symbol, result))
        return tuple(sorted(rows, key=lambda item: (item.observed_at, item.symbol)))

    @classmethod
    def standard_price_features(cls, price_field: str = "close") -> "FeaturePipeline":
        pipeline = cls()

        def price(values: Sequence[Observation], index: int) -> Optional[Decimal]:
            return values[index].fields.get(price_field)

        def return_one(values: Sequence[Observation], index: int) -> Optional[Decimal]:
            current = values[index].fields.get(price_field)
            prior = values[index - 1].fields.get(price_field) if index else None
            if current is None or prior in {None, Decimal("0")}:
                return None
            return current / prior - Decimal("1")

        def moving_average(window: int) -> FeatureFunction:
            def calculate(values: Sequence[Observation], index: int) -> Optional[Decimal]:
                selected = [item.fields.get(price_field) for item in values[index - window + 1:index + 1]]
                if len(selected) != window or any(item is None for item in selected):
                    return None
                return sum((item for item in selected if item is not None), Decimal("0")) / Decimal(window)
            return calculate

        def rolling_volatility(window: int) -> FeatureFunction:
            def calculate(values: Sequence[Observation], index: int) -> Optional[Decimal]:
                returns = []
                start = index - window + 1
                for position in range(max(1, start), index + 1):
                    current = values[position].fields.get(price_field)
                    prior = values[position - 1].fields.get(price_field)
                    if current is None or prior in {None, Decimal("0")}:
                        return None
                    returns.append(current / prior - Decimal("1"))
                if len(returns) < 2:
                    return None
                mean = sum(returns, Decimal("0")) / Decimal(len(returns))
                variance = sum(((item - mean) ** 2 for item in returns), Decimal("0")) / Decimal(len(returns) - 1)
                return Decimal(str(float(variance) ** 0.5))
            return calculate

        pipeline.add(FeatureDefinition("price", price, 1))
        pipeline.add(FeatureDefinition("return_1", return_one, 2, ("price",)))
        pipeline.add(FeatureDefinition("ma_5", moving_average(5), 5, ("price",)))
        pipeline.add(FeatureDefinition("ma_20", moving_average(20), 20, ("price",)))
        pipeline.add(FeatureDefinition("volatility_20", rolling_volatility(20), 21, ("return_1",)))
        return pipeline
