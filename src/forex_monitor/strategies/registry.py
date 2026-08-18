"""Name-based strategy registration without global mutation."""

from dataclasses import dataclass, field
from typing import Dict, Iterable, Tuple

from forex_monitor.strategies.base import Strategy


@dataclass
class StrategyRegistry:
    _strategies: Dict[str, Strategy] = field(default_factory=dict)

    def register(self, strategy: Strategy) -> None:
        name = strategy.name.strip().lower()
        if not name:
            raise ValueError("strategy name cannot be blank")
        if name in self._strategies:
            raise ValueError(f"strategy {name} is already registered")
        self._strategies[name] = strategy

    def register_all(self, strategies: Iterable[Strategy]) -> None:
        for strategy in strategies:
            self.register(strategy)

    def get(self, name: str) -> Strategy:
        normalized = name.strip().lower()
        try:
            return self._strategies[normalized]
        except KeyError as error:
            raise KeyError(f"strategy {normalized} is not registered") from error

    def names(self) -> Tuple[str, ...]:
        return tuple(sorted(self._strategies))
