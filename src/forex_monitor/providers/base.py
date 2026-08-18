"""Interfaces and shared value objects for external market-data providers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence, Tuple

from forex_monitor.models import Quote, ensure_utc

Clock = Callable[[], datetime]
"""Callable returning an aware UTC datetime."""

Sleeper = Callable[[float], None]
"""Injectable sleep operation used by retry loops."""

IdentifierFactory = Callable[[], str]
"""Callable returning a stable-format unique identifier."""


@dataclass(frozen=True)
class TransportRequest:
    """Provider-agnostic HTTP request."""

    method: str
    url: str
    headers: Mapping[str, str]
    query: Mapping[str, str]
    timeout_seconds: float

    def __post_init__(self) -> None:
        method = self.method.strip().upper()
        if method not in {"GET", "POST"}:
            raise ValueError("transport method must be GET or POST")
        if not self.url.startswith(("http://", "https://")):
            raise ValueError("transport URL must use http or https")
        if self.timeout_seconds <= 0:
            raise ValueError("transport timeout must be positive")
        object.__setattr__(self, "method", method)
        object.__setattr__(self, "headers", dict(self.headers))
        object.__setattr__(self, "query", dict(self.query))


@dataclass(frozen=True)
class TransportResponse:
    """Minimal HTTP response returned to provider clients."""

    status: int
    headers: Mapping[str, str]
    body: bytes
    elapsed_seconds: float

    def __post_init__(self) -> None:
        if self.status < 100 or self.status > 599:
            raise ValueError("transport status must be a valid HTTP code")
        if self.elapsed_seconds < 0:
            raise ValueError("transport elapsed time cannot be negative")
        object.__setattr__(
            self, "headers", {key.lower(): value for key, value in self.headers.items()}
        )

    @property
    def content_type(self) -> Optional[str]:
        value = self.headers.get("content-type")
        return value.split(";", 1)[0].strip().lower() if value else None

    @property
    def text(self) -> str:
        content_type = self.headers.get("content-type", "")
        charset = "utf-8"
        for item in content_type.split(";")[1:]:
            if "=" in item:
                key, value = item.split("=", 1)
                if key.strip().lower() == "charset":
                    charset = value.strip().strip('"')
        return self.body.decode(charset, errors="strict")


class HttpTransport(Protocol):
    """Synchronous HTTP transport used by the dependency-free provider."""

    def send(self, request: TransportRequest) -> TransportResponse: ...


@dataclass(frozen=True)
class RawProviderResponse:
    """Validated raw response before provider-specific normalization."""

    provider: str
    requested_at: datetime
    received_at: datetime
    status: int
    payload: Any
    headers: Mapping[str, str]
    attempts: int
    elapsed_seconds: float

    def __post_init__(self) -> None:
        provider = self.provider.strip().lower()
        if not provider:
            raise ValueError("provider cannot be blank")
        requested_at = ensure_utc(self.requested_at, "requested at")
        received_at = ensure_utc(self.received_at, "received at")
        if received_at < requested_at:
            raise ValueError("received at cannot precede requested at")
        if self.status < 100 or self.status > 599:
            raise ValueError("status must be a valid HTTP code")
        if self.attempts < 1:
            raise ValueError("attempt count must be positive")
        if self.elapsed_seconds < 0:
            raise ValueError("elapsed time cannot be negative")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "requested_at", requested_at)
        object.__setattr__(self, "received_at", received_at)
        object.__setattr__(self, "headers", dict(self.headers))


@dataclass(frozen=True)
class ProviderHealth:
    """Observable status returned by an optional provider health check."""

    provider: str
    healthy: bool
    checked_at: datetime
    latency_seconds: Optional[float] = None
    message: Optional[str] = None

    def __post_init__(self) -> None:
        provider = self.provider.strip().lower()
        if not provider:
            raise ValueError("provider cannot be blank")
        if self.latency_seconds is not None and self.latency_seconds < 0:
            raise ValueError("latency cannot be negative")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "checked_at", ensure_utc(self.checked_at, "checked at"))


class MarketDataProvider(Protocol):
    """Public contract consumed by the ingestion service."""

    @property
    def name(self) -> str: ...

    def fetch_quotes(self, symbols: Sequence[str]) -> Tuple[Quote, ...]: ...

    def health(self) -> ProviderHealth: ...


def exponential_backoff(
    initial_seconds: float,
    attempt: int,
    *,
    maximum_seconds: float = 30.0,
) -> float:
    """Return a bounded exponential retry delay for a one-based attempt."""

    if initial_seconds < 0:
        raise ValueError("initial backoff cannot be negative")
    if attempt < 1:
        raise ValueError("attempt must be positive")
    if maximum_seconds < 0:
        raise ValueError("maximum backoff cannot be negative")
    return float(min(initial_seconds * (2 ** (attempt - 1)), maximum_seconds))


def parse_retry_after(value: Optional[str]) -> Optional[float]:
    """Parse a numeric Retry-After header; HTTP dates are left to callers."""

    if value is None:
        return None
    try:
        seconds = float(value.strip())
    except (TypeError, ValueError):
        return None
    return max(seconds, 0.0)


def normalize_symbols(symbols: Sequence[str]) -> Tuple[str, ...]:
    """Normalize provider requests while preserving the caller's order."""

    normalized = []
    seen = set()
    for symbol in symbols:
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("provider symbols must be non-empty strings")
        item = symbol.strip().upper().replace("/", "")
        if item not in seen:
            normalized.append(item)
            seen.add(item)
    if not normalized:
        raise ValueError("at least one provider symbol is required")
    return tuple(normalized)


ExceptionClassifier = Callable[[BaseException], bool]


def retryable_exception(error: BaseException) -> bool:
    """Default classifier for transient network errors."""

    return isinstance(error, (TimeoutError, ConnectionError, OSError))
