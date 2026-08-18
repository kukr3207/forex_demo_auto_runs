"""Deterministic provider for local development and automated tests."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Deque, Iterable, Mapping, Optional, Sequence, Tuple, Union, cast

from forex_monitor.errors import ProviderError
from forex_monitor.models import Quote, ensure_utc
from forex_monitor.providers.base import ProviderHealth, normalize_symbols

FixtureItem = Union[Sequence[Quote], BaseException]


@dataclass
class FixtureProvider:
    """Return queued quote batches without network access.

    A batch can be reused after the queue is exhausted, which makes the default
    behavior suitable for schedulers. Set repeat_last to false when exhaustion
    should be an explicit test failure.
    """

    batches: Iterable[FixtureItem]
    provider_name: str = "fixture"
    repeat_last: bool = True
    now: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        self.provider_name = self.provider_name.strip().lower()
        if not self.provider_name:
            raise ValueError("fixture provider name cannot be blank")
        self._queue: Deque[FixtureItem] = deque(self.batches)
        self._last: Optional[FixtureItem] = None
        self.calls: list[Tuple[str, ...]] = []

    @property
    def name(self) -> str:
        return self.provider_name

    def fetch_quotes(self, symbols: Sequence[str]) -> Tuple[Quote, ...]:
        requested = normalize_symbols(symbols)
        self.calls.append(requested)
        if self._queue:
            item = self._queue.popleft()
            self._last = item
        elif self.repeat_last and self._last is not None:
            item = self._last
        else:
            raise ProviderError("fixture provider has no remaining batches")
        if isinstance(item, BaseException):
            raise item
        by_symbol = {quote.symbol: quote for quote in item}
        result = []
        for symbol in requested:
            quote = by_symbol.get(symbol)
            if quote is not None:
                if quote.provider != self.provider_name:
                    quote = Quote(
                        symbol=quote.symbol,
                        bid=quote.bid,
                        ask=quote.ask,
                        observed_at=quote.observed_at,
                        provider=self.provider_name,
                        source_id=quote.source_id,
                        volume=quote.volume,
                        metadata=quote.metadata,
                    )
                result.append(quote)
        return tuple(result)

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider=self.provider_name,
            healthy=bool(self._queue or (self.repeat_last and self._last is not None)),
            checked_at=ensure_utc(self.now()),
            latency_seconds=0.0,
            message=None if self._queue or self._last is not None else "no fixture batches",
        )

    @classmethod
    def from_mappings(
        cls,
        batches: Iterable[Iterable[Mapping[str, object]]],
        *,
        provider_name: str = "fixture",
        repeat_last: bool = True,
    ) -> FixtureProvider:
        normalized = []
        for batch in batches:
            normalized.append(
                tuple(
                    Quote(
                        symbol=str(item.get("symbol", "")),
                        bid=item.get("bid"),  # type: ignore[arg-type]
                        ask=item.get("ask"),  # type: ignore[arg-type]
                        observed_at=cast(
                            datetime,
                            item["observed_at"]
                            if isinstance(item.get("observed_at"), datetime)
                            else datetime.fromisoformat(str(item.get("observed_at"))),
                        ),
                        provider=provider_name,
                        source_id=(
                            str(item["source_id"]) if item.get("source_id") is not None else None
                        ),
                        volume=item.get("volume"),  # type: ignore[arg-type]
                    )
                    for item in batch
                )
            )
        return cls(normalized, provider_name=provider_name, repeat_last=repeat_last)
