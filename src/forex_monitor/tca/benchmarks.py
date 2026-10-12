"""Market observations and reproducible execution benchmarks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Dict, Iterable, Mapping, Optional, Sequence, Tuple

from forex_monitor.errors import StorageConflictError, ValidationError
from forex_monitor.models import as_decimal, ensure_utc, normalize_symbol


class BenchmarkKind(str, Enum):
    ARRIVAL_MID = "arrival_mid"
    ARRIVAL_SIDE = "arrival_side"
    CLOSING_MID = "closing_mid"
    TWAP = "twap"
    VWAP = "vwap"


@dataclass(frozen=True)
class MarketObservation:
    observation_id: str
    symbol: str
    observed_at: datetime
    bid: Decimal
    ask: Decimal
    bid_size: Decimal = Decimal("0")
    ask_size: Decimal = Decimal("0")
    traded_volume: Decimal = Decimal("0")
    source: str = "consolidated"

    def __post_init__(self) -> None:
        identifier = self.observation_id.strip()
        source = self.source.strip()
        if not identifier or not source:
            raise ValidationError("observation id and source cannot be blank")
        bid = as_decimal(self.bid, "observation bid", positive=True)
        ask = as_decimal(self.ask, "observation ask", positive=True)
        if bid > ask:
            raise ValidationError("observation bid cannot exceed ask")
        values = {
            "bid_size": as_decimal(self.bid_size, "bid size"),
            "ask_size": as_decimal(self.ask_size, "ask size"),
            "traded_volume": as_decimal(self.traded_volume, "traded volume"),
        }
        if any(value < 0 for value in values.values()):
            raise ValidationError("observation sizes and volume cannot be negative")
        object.__setattr__(self, "observation_id", identifier)
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(self, "observed_at", ensure_utc(self.observed_at, "observation time"))
        object.__setattr__(self, "bid", bid)
        object.__setattr__(self, "ask", ask)
        object.__setattr__(self, "bid_size", values["bid_size"])
        object.__setattr__(self, "ask_size", values["ask_size"])
        object.__setattr__(self, "traded_volume", values["traded_volume"])
        object.__setattr__(self, "source", source)

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / Decimal("2")

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid


@dataclass(frozen=True)
class BenchmarkValue:
    symbol: str
    kind: BenchmarkKind
    price: Decimal
    starts_at: datetime
    ends_at: datetime
    observation_count: int
    total_volume: Decimal

    def __post_init__(self) -> None:
        if self.observation_count < 1:
            raise ValidationError("benchmark requires at least one observation")
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(self, "kind", self.kind if isinstance(self.kind, BenchmarkKind) else BenchmarkKind(self.kind))
        object.__setattr__(self, "price", as_decimal(self.price, "benchmark price", positive=True))
        object.__setattr__(self, "starts_at", ensure_utc(self.starts_at, "benchmark start"))
        object.__setattr__(self, "ends_at", ensure_utc(self.ends_at, "benchmark end"))
        object.__setattr__(self, "total_volume", as_decimal(self.total_volume, "benchmark volume"))
        if self.ends_at < self.starts_at:
            raise ValidationError("benchmark end cannot precede start")


class BenchmarkBook:
    def __init__(self) -> None:
        self._observations: Dict[str, MarketObservation] = {}
        self._by_symbol: Dict[str, list[str]] = {}

    def record(self, observation: MarketObservation) -> MarketObservation:
        existing = self._observations.get(observation.observation_id)
        if existing is not None:
            if existing == observation:
                return existing
            raise StorageConflictError("observation id was reused with different market data")
        self._observations[observation.observation_id] = observation
        identifiers = self._by_symbol.setdefault(observation.symbol, [])
        identifiers.append(observation.observation_id)
        identifiers.sort(key=lambda value: (
            self._observations[value].observed_at,
            value,
        ))
        return observation

    def record_many(self, observations: Iterable[MarketObservation]) -> Tuple[MarketObservation, ...]:
        staged = tuple(observations)
        seen: Dict[str, MarketObservation] = {}
        for observation in staged:
            duplicate = seen.get(observation.observation_id)
            if duplicate is not None and duplicate != observation:
                raise StorageConflictError("batch contains conflicting observation ids")
            existing = self._observations.get(observation.observation_id)
            if existing is not None and existing != observation:
                raise StorageConflictError("observation id was reused with different market data")
            seen[observation.observation_id] = observation
        return tuple(self.record(item) for item in staged)

    def window(self, symbol: str, starts_at: datetime, ends_at: datetime) -> Tuple[MarketObservation, ...]:
        normalized = normalize_symbol(symbol)
        start = ensure_utc(starts_at, "window start")
        end = ensure_utc(ends_at, "window end")
        if end < start:
            raise ValidationError("market window end cannot precede start")
        return tuple(
            self._observations[identifier]
            for identifier in self._by_symbol.get(normalized, [])
            if start <= self._observations[identifier].observed_at <= end
        )

    def at_or_before(self, symbol: str, at: datetime) -> Optional[MarketObservation]:
        normalized = normalize_symbol(symbol)
        boundary = ensure_utc(at, "benchmark boundary")
        candidates = tuple(
            self._observations[identifier]
            for identifier in self._by_symbol.get(normalized, [])
            if self._observations[identifier].observed_at <= boundary
        )
        return candidates[-1] if candidates else None

    def calculate(
        self,
        symbol: str,
        kind: BenchmarkKind,
        starts_at: datetime,
        ends_at: datetime,
        side: Optional[str] = None,
    ) -> BenchmarkValue:
        normalized = normalize_symbol(symbol)
        parsed_kind = kind if isinstance(kind, BenchmarkKind) else BenchmarkKind(kind)
        start = ensure_utc(starts_at, "benchmark start")
        end = ensure_utc(ends_at, "benchmark end")
        observations = self.window(normalized, start, end)
        if parsed_kind in {BenchmarkKind.ARRIVAL_MID, BenchmarkKind.ARRIVAL_SIDE}:
            arrival = self.at_or_before(normalized, start)
            if arrival is None:
                raise ValidationError("arrival benchmark has no preceding market observation")
            if parsed_kind is BenchmarkKind.ARRIVAL_MID:
                price = arrival.mid
            else:
                normalized_side = (side or "").strip().lower()
                if normalized_side not in {"buy", "sell"}:
                    raise ValidationError("side benchmark requires buy or sell")
                price = arrival.ask if normalized_side == "buy" else arrival.bid
            return BenchmarkValue(normalized, parsed_kind, price, start, start, 1, arrival.traded_volume)
        if not observations:
            raise ValidationError("benchmark window contains no observations")
        if parsed_kind is BenchmarkKind.CLOSING_MID:
            closing = observations[-1]
            price = closing.mid
        elif parsed_kind is BenchmarkKind.TWAP:
            price = sum((item.mid for item in observations), Decimal("0")) / Decimal(len(observations))
        elif parsed_kind is BenchmarkKind.VWAP:
            volume = sum((item.traded_volume for item in observations), Decimal("0"))
            if volume <= 0:
                raise ValidationError("VWAP benchmark requires positive traded volume")
            price = sum((item.mid * item.traded_volume for item in observations), Decimal("0")) / volume
        else:
            raise ValidationError("benchmark kind is not supported")
        return BenchmarkValue(
            normalized,
            parsed_kind,
            price,
            observations[0].observed_at,
            observations[-1].observed_at,
            len(observations),
            sum((item.traded_volume for item in observations), Decimal("0")),
        )

    def symbols(self) -> Tuple[str, ...]:
        return tuple(sorted(self._by_symbol))

