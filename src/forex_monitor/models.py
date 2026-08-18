"""Validated immutable domain models.

All temporal fields are timezone-aware UTC values. Decimal is used for quoted
prices and monetary calculations so callers never inherit binary floating-point
rounding from provider payloads.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Dict, Iterable, Mapping, Optional, Sequence, Tuple, cast

from forex_monitor.errors import ValidationError

_SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9._-]{1,19}$")
_CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")


def utc_now() -> datetime:
    """Return an aware UTC time; injected clocks should return the same shape."""

    return datetime.now(timezone.utc)


def ensure_utc(value: datetime, field_name: str = "timestamp") -> datetime:
    """Validate and normalize an aware datetime to UTC."""

    if not isinstance(value, datetime):
        raise ValidationError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{field_name} must include a timezone")
    return value.astimezone(timezone.utc)


def parse_timestamp(value: object, field_name: str = "timestamp") -> datetime:
    """Parse an ISO-8601 timestamp and normalize it to UTC."""

    if isinstance(value, datetime):
        return ensure_utc(value, field_name)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field_name} must be an ISO timestamp")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError(f"{field_name} must be an ISO timestamp") from exc
    return ensure_utc(parsed, field_name)


def format_timestamp(value: datetime) -> str:
    """Return the package's canonical UTC timestamp representation."""

    return ensure_utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def as_decimal(value: object, field_name: str, *, positive: bool = False) -> Decimal:
    """Coerce a finite numeric value to Decimal without float artifacts."""

    if isinstance(value, bool) or value is None:
        raise ValidationError(f"{field_name} must be numeric")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValidationError(f"{field_name} must be finite")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationError(f"{field_name} must be numeric") from exc
    if not number.is_finite():
        raise ValidationError(f"{field_name} must be finite")
    if positive and number <= 0:
        raise ValidationError(f"{field_name} must be positive")
    return number


def _non_negative_decimal(value: object, field_name: str) -> Decimal:
    number = as_decimal(value, field_name)
    if number < 0:
        raise ValidationError(f"{field_name} cannot be negative")
    return number


def _identifier(value: object, field_name: str = "id") -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field_name} must be a string")
    text = value.strip()
    if not _IDENTIFIER_PATTERN.fullmatch(text):
        raise ValidationError(f"{field_name} has an invalid format")
    return text


def _symbol(value: object) -> str:
    if not isinstance(value, str):
        raise ValidationError("symbol must be a string")
    text = value.strip().upper().replace("/", "")
    if not _SYMBOL_PATTERN.fullmatch(text):
        raise ValidationError("symbol has an invalid format")
    return text


def normalize_symbol(value: object) -> str:
    """Return the public canonical representation of an instrument symbol."""

    return _symbol(value)


def _currency(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field_name} must be a currency code")
    text = value.strip().upper()
    if not _CURRENCY_PATTERN.fullmatch(text):
        raise ValidationError(f"{field_name} must be a three-letter currency code")
    return text


def _metadata(value: Mapping[str, object]) -> Mapping[str, object]:
    result: Dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            raise ValidationError("metadata keys must be non-empty strings")
        if not isinstance(item, (str, int, float, bool, type(None))):
            raise ValidationError("metadata values must be JSON scalars")
        if isinstance(item, float) and not math.isfinite(item):
            raise ValidationError("metadata numbers must be finite")
        result[key.strip()] = item
    return result


class Timeframe(str, Enum):
    """Supported fixed market-data intervals."""

    MINUTE_1 = "1m"
    MINUTE_5 = "5m"
    MINUTE_15 = "15m"
    MINUTE_30 = "30m"
    HOUR_1 = "1h"
    HOUR_4 = "4h"
    DAY_1 = "1d"

    @property
    def seconds(self) -> int:
        return {
            Timeframe.MINUTE_1: 60,
            Timeframe.MINUTE_5: 300,
            Timeframe.MINUTE_15: 900,
            Timeframe.MINUTE_30: 1800,
            Timeframe.HOUR_1: 3600,
            Timeframe.HOUR_4: 14_400,
            Timeframe.DAY_1: 86_400,
        }[self]

    @classmethod
    def parse(cls, value: object) -> Timeframe:
        if isinstance(value, cls):
            return value
        if not isinstance(value, str):
            raise ValidationError("timeframe must be a string")
        try:
            return cls(value.strip().lower())
        except ValueError as exc:
            raise ValidationError("timeframe is not supported") from exc


class Signal(str, Enum):
    """Directional analytics result."""

    STRONG_SELL = "strong_sell"
    SELL = "sell"
    NEUTRAL = "neutral"
    BUY = "buy"
    STRONG_BUY = "strong_buy"


class AlertOperator(str, Enum):
    """Comparison operators supported by alert rules."""

    GREATER_THAN = "gt"
    GREATER_OR_EQUAL = "gte"
    LESS_THAN = "lt"
    LESS_OR_EQUAL = "lte"
    CROSSES_ABOVE = "crosses_above"
    CROSSES_BELOW = "crosses_below"

    @classmethod
    def parse(cls, value: object) -> AlertOperator:
        if isinstance(value, cls):
            return value
        if not isinstance(value, str):
            raise ValidationError("alert operator must be a string")
        try:
            return cls(value.strip().lower())
        except ValueError as exc:
            raise ValidationError("alert operator is not supported") from exc


class AlertStatus(str, Enum):
    """Delivery lifecycle of an emitted alert."""

    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"
    ACKNOWLEDGED = "acknowledged"


class RunStatus(str, Enum):
    """Lifecycle state for an ingestion run."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass(frozen=True)
class Instrument:
    """Tradeable currency pair metadata."""

    symbol: str
    base_currency: str
    quote_currency: str
    precision: int = 5
    pip_size: Decimal = Decimal("0.0001")
    active: bool = True
    display_name: Optional[str] = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        symbol = _symbol(self.symbol)
        base = _currency(self.base_currency, "base currency")
        quote = _currency(self.quote_currency, "quote currency")
        if base == quote:
            raise ValidationError("base and quote currencies must differ")
        if self.precision < 0 or self.precision > 12:
            raise ValidationError("precision must be between 0 and 12")
        pip_size = as_decimal(self.pip_size, "pip size", positive=True)
        display_name = self.display_name.strip() if self.display_name else f"{base}/{quote}"
        if len(display_name) > 80:
            raise ValidationError("display name cannot exceed 80 characters")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "base_currency", base)
        object.__setattr__(self, "quote_currency", quote)
        object.__setattr__(self, "pip_size", pip_size)
        object.__setattr__(self, "display_name", display_name)
        object.__setattr__(self, "metadata", _metadata(self.metadata))

    @classmethod
    def from_symbol(
        cls,
        symbol: str,
        *,
        precision: Optional[int] = None,
        pip_size: Optional[object] = None,
    ) -> Instrument:
        normalized = _symbol(symbol)
        if len(normalized) != 6 or not normalized.isalpha():
            raise ValidationError("currency-pair symbols must contain six letters")
        quote = normalized[3:]
        inferred_precision = 3 if quote == "JPY" else 5
        inferred_pip = Decimal("0.01") if quote == "JPY" else Decimal("0.0001")
        return cls(
            symbol=normalized,
            base_currency=normalized[:3],
            quote_currency=quote,
            precision=inferred_precision if precision is None else precision,
            pip_size=inferred_pip if pip_size is None else as_decimal(pip_size, "pip size"),
        )

    def as_dict(self) -> Mapping[str, object]:
        return {
            "symbol": self.symbol,
            "baseCurrency": self.base_currency,
            "quoteCurrency": self.quote_currency,
            "precision": self.precision,
            "pipSize": str(self.pip_size),
            "active": self.active,
            "displayName": self.display_name,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class Quote:
    """Bid/ask quote for a single instrument at an instant."""

    symbol: str
    bid: Decimal
    ask: Decimal
    observed_at: datetime
    provider: str
    source_id: Optional[str] = None
    volume: Optional[Decimal] = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        symbol = _symbol(self.symbol)
        bid = as_decimal(self.bid, "bid", positive=True)
        ask = as_decimal(self.ask, "ask", positive=True)
        if ask < bid:
            raise ValidationError("ask cannot be lower than bid")
        provider = self.provider.strip().lower() if isinstance(self.provider, str) else ""
        if not provider or len(provider) > 60:
            raise ValidationError("provider must be a 1-60 character string")
        source_id = None
        if self.source_id is not None:
            source_id = _identifier(self.source_id, "source id")
        volume = None
        if self.volume is not None:
            volume = _non_negative_decimal(self.volume, "volume")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "bid", bid)
        object.__setattr__(self, "ask", ask)
        object.__setattr__(self, "observed_at", ensure_utc(self.observed_at, "observed at"))
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "volume", volume)
        object.__setattr__(self, "metadata", _metadata(self.metadata))

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / Decimal("2")

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid

    def spread_pips(self, instrument: Instrument) -> Decimal:
        if instrument.symbol != self.symbol:
            raise ValidationError("instrument and quote symbols must match")
        return self.spread / instrument.pip_size

    def as_dict(self) -> Mapping[str, object]:
        return {
            "symbol": self.symbol,
            "bid": str(self.bid),
            "ask": str(self.ask),
            "mid": str(self.mid),
            "spread": str(self.spread),
            "observedAt": format_timestamp(self.observed_at),
            "provider": self.provider,
            "sourceId": self.source_id,
            "volume": str(self.volume) if self.volume is not None else None,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> Quote:
        metadata_value = value.get("metadata", {})
        metadata = (
            cast(Mapping[str, object], metadata_value)
            if isinstance(metadata_value, Mapping)
            else {}
        )
        return cls(
            symbol=str(value.get("symbol", "")),
            bid=as_decimal(value.get("bid"), "bid"),
            ask=as_decimal(value.get("ask"), "ask"),
            observed_at=parse_timestamp(
                value.get("observedAt", value.get("observed_at")),
                "observed at",
            ),
            provider=str(value.get("provider", "")),
            source_id=str(value["sourceId"]) if value.get("sourceId") is not None else None,
            volume=(
                as_decimal(value["volume"], "volume") if value.get("volume") is not None else None
            ),
            metadata=metadata,
        )


@dataclass(frozen=True)
class Candle:
    """OHLCV bucket for a fixed timeframe."""

    symbol: str
    timeframe: Timeframe
    opened_at: datetime
    closed_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal = Decimal("0")
    sample_count: int = 1
    complete: bool = True

    def __post_init__(self) -> None:
        symbol = _symbol(self.symbol)
        timeframe = Timeframe.parse(self.timeframe)
        opened_at = ensure_utc(self.opened_at, "opened at")
        closed_at = ensure_utc(self.closed_at, "closed at")
        if closed_at <= opened_at:
            raise ValidationError("closed at must be later than opened at")
        open_price = as_decimal(self.open, "open", positive=True)
        high = as_decimal(self.high, "high", positive=True)
        low = as_decimal(self.low, "low", positive=True)
        close = as_decimal(self.close, "close", positive=True)
        if high < max(open_price, close, low):
            raise ValidationError("high must be the greatest candle price")
        if low > min(open_price, close, high):
            raise ValidationError("low must be the smallest candle price")
        if self.sample_count < 1:
            raise ValidationError("sample count must be positive")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "timeframe", timeframe)
        object.__setattr__(self, "opened_at", opened_at)
        object.__setattr__(self, "closed_at", closed_at)
        object.__setattr__(self, "open", open_price)
        object.__setattr__(self, "high", high)
        object.__setattr__(self, "low", low)
        object.__setattr__(self, "close", close)
        object.__setattr__(self, "volume", _non_negative_decimal(self.volume, "volume"))

    @property
    def direction(self) -> int:
        return 1 if self.close > self.open else -1 if self.close < self.open else 0

    @property
    def body(self) -> Decimal:
        return abs(self.close - self.open)

    @property
    def range(self) -> Decimal:
        return self.high - self.low

    def as_dict(self) -> Mapping[str, object]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe.value,
            "openedAt": format_timestamp(self.opened_at),
            "closedAt": format_timestamp(self.closed_at),
            "open": str(self.open),
            "high": str(self.high),
            "low": str(self.low),
            "close": str(self.close),
            "volume": str(self.volume),
            "sampleCount": self.sample_count,
            "complete": self.complete,
        }


@dataclass(frozen=True)
class MarketSnapshot:
    """Atomic provider observation containing one or more quotes."""

    id: str
    provider: str
    observed_at: datetime
    quotes: Tuple[Quote, ...]
    received_at: datetime
    checksum: str
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        snapshot_id = _identifier(self.id, "snapshot id")
        provider = self.provider.strip().lower() if isinstance(self.provider, str) else ""
        if not provider:
            raise ValidationError("provider cannot be blank")
        quotes = tuple(self.quotes)
        if not quotes:
            raise ValidationError("a market snapshot requires at least one quote")
        seen = set()
        for quote in quotes:
            if not isinstance(quote, Quote):
                raise ValidationError("snapshot quotes must be Quote values")
            if quote.provider != provider:
                raise ValidationError("snapshot and quote providers must match")
            if quote.symbol in seen:
                raise ValidationError("snapshot symbols must be unique")
            seen.add(quote.symbol)
        checksum = self.checksum.strip().lower() if isinstance(self.checksum, str) else ""
        if not re.fullmatch(r"[0-9a-f]{64}", checksum):
            raise ValidationError("snapshot checksum must be a SHA-256 hex digest")
        observed_at = ensure_utc(self.observed_at, "observed at")
        received_at = ensure_utc(self.received_at, "received at")
        if received_at < observed_at:
            raise ValidationError("received at cannot precede observed at")
        object.__setattr__(self, "id", snapshot_id)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "received_at", received_at)
        object.__setattr__(self, "quotes", tuple(sorted(quotes, key=lambda quote: quote.symbol)))
        object.__setattr__(self, "checksum", checksum)
        object.__setattr__(self, "metadata", _metadata(self.metadata))

    def quote_for(self, symbol: str) -> Optional[Quote]:
        normalized = _symbol(symbol)
        return next((quote for quote in self.quotes if quote.symbol == normalized), None)

    def as_dict(self) -> Mapping[str, object]:
        return {
            "id": self.id,
            "provider": self.provider,
            "observedAt": format_timestamp(self.observed_at),
            "receivedAt": format_timestamp(self.received_at),
            "checksum": self.checksum,
            "quotes": [quote.as_dict() for quote in self.quotes],
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class IndicatorValue:
    """Named numeric analytics observation."""

    symbol: str
    name: str
    timeframe: Timeframe
    observed_at: datetime
    value: Decimal
    parameters: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        name = self.name.strip().lower() if isinstance(self.name, str) else ""
        if not name or len(name) > 80:
            raise ValidationError("indicator name must contain 1-80 characters")
        object.__setattr__(self, "symbol", _symbol(self.symbol))
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "timeframe", Timeframe.parse(self.timeframe))
        object.__setattr__(self, "observed_at", ensure_utc(self.observed_at))
        object.__setattr__(self, "value", as_decimal(self.value, "indicator value"))
        object.__setattr__(self, "parameters", _metadata(self.parameters))

    def as_dict(self) -> Mapping[str, object]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "timeframe": self.timeframe.value,
            "observedAt": format_timestamp(self.observed_at),
            "value": str(self.value),
            "parameters": dict(self.parameters),
        }


@dataclass(frozen=True)
class AnalysisResult:
    """A point-in-time set of indicators and an optional directional signal."""

    symbol: str
    timeframe: Timeframe
    observed_at: datetime
    indicators: Tuple[IndicatorValue, ...]
    signal: Signal = Signal.NEUTRAL
    score: Decimal = Decimal("0")
    reasons: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        symbol = _symbol(self.symbol)
        timeframe = Timeframe.parse(self.timeframe)
        indicators = tuple(self.indicators)
        seen = set()
        for indicator in indicators:
            if indicator.symbol != symbol or indicator.timeframe != timeframe:
                raise ValidationError("analysis and indicator dimensions must match")
            if indicator.name in seen:
                raise ValidationError("analysis indicator names must be unique")
            seen.add(indicator.name)
        score = as_decimal(self.score, "analysis score")
        if score < -100 or score > 100:
            raise ValidationError("analysis score must be between -100 and 100")
        reasons = tuple(reason.strip() for reason in self.reasons if reason.strip())
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "timeframe", timeframe)
        object.__setattr__(self, "observed_at", ensure_utc(self.observed_at))
        object.__setattr__(self, "indicators", indicators)
        object.__setattr__(self, "score", score)
        object.__setattr__(self, "reasons", reasons)

    def value(self, name: str) -> Optional[Decimal]:
        normalized = name.strip().lower()
        match = next((item for item in self.indicators if item.name == normalized), None)
        return match.value if match else None

    def as_dict(self) -> Mapping[str, object]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe.value,
            "observedAt": format_timestamp(self.observed_at),
            "signal": self.signal.value,
            "score": str(self.score),
            "reasons": list(self.reasons),
            "indicators": [indicator.as_dict() for indicator in self.indicators],
        }


@dataclass(frozen=True)
class AlertRule:
    """Persistent rule evaluated against a quote or indicator field."""

    id: str
    name: str
    symbol: str
    metric: str
    operator: AlertOperator
    threshold: Decimal
    cooldown_seconds: int = 3600
    enabled: bool = True
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    last_triggered_at: Optional[datetime] = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        rule_id = _identifier(self.id, "rule id")
        name = self.name.strip() if isinstance(self.name, str) else ""
        if not name or len(name) > 100:
            raise ValidationError("rule name must contain 1-100 characters")
        metric = self.metric.strip().lower() if isinstance(self.metric, str) else ""
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,79}", metric):
            raise ValidationError("rule metric has an invalid format")
        if self.cooldown_seconds < 0 or self.cooldown_seconds > 31_536_000:
            raise ValidationError("cooldown must be between 0 and 31536000 seconds")
        created_at = ensure_utc(self.created_at, "created at")
        updated_at = ensure_utc(self.updated_at, "updated at")
        if updated_at < created_at:
            raise ValidationError("updated at cannot precede created at")
        last_triggered_at = (
            ensure_utc(self.last_triggered_at, "last triggered at")
            if self.last_triggered_at
            else None
        )
        object.__setattr__(self, "id", rule_id)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "symbol", _symbol(self.symbol))
        object.__setattr__(self, "metric", metric)
        object.__setattr__(self, "operator", AlertOperator.parse(self.operator))
        object.__setattr__(self, "threshold", as_decimal(self.threshold, "threshold"))
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "updated_at", updated_at)
        object.__setattr__(self, "last_triggered_at", last_triggered_at)
        object.__setattr__(self, "metadata", _metadata(self.metadata))

    def with_trigger(self, triggered_at: datetime) -> AlertRule:
        instant = ensure_utc(triggered_at, "triggered at")
        return replace(self, last_triggered_at=instant, updated_at=max(self.updated_at, instant))

    def as_dict(self) -> Mapping[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "symbol": self.symbol,
            "metric": self.metric,
            "operator": self.operator.value,
            "threshold": str(self.threshold),
            "cooldownSeconds": self.cooldown_seconds,
            "enabled": self.enabled,
            "createdAt": format_timestamp(self.created_at),
            "updatedAt": format_timestamp(self.updated_at),
            "lastTriggeredAt": (
                format_timestamp(self.last_triggered_at) if self.last_triggered_at else None
            ),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class Alert:
    """Immutable alert occurrence produced by a rule evaluation."""

    id: str
    rule_id: str
    symbol: str
    metric: str
    observed_value: Decimal
    threshold: Decimal
    operator: AlertOperator
    triggered_at: datetime
    message: str
    status: AlertStatus = AlertStatus.PENDING
    delivered_at: Optional[datetime] = None
    acknowledged_at: Optional[datetime] = None
    delivery_error: Optional[str] = None

    def __post_init__(self) -> None:
        message = self.message.strip() if isinstance(self.message, str) else ""
        if not message or len(message) > 500:
            raise ValidationError("alert message must contain 1-500 characters")
        triggered_at = ensure_utc(self.triggered_at, "triggered at")
        delivered_at = ensure_utc(self.delivered_at, "delivered at") if self.delivered_at else None
        acknowledged_at = (
            ensure_utc(self.acknowledged_at, "acknowledged at") if self.acknowledged_at else None
        )
        if delivered_at and delivered_at < triggered_at:
            raise ValidationError("delivered at cannot precede triggered at")
        if acknowledged_at and acknowledged_at < triggered_at:
            raise ValidationError("acknowledged at cannot precede triggered at")
        if self.delivery_error is not None and len(self.delivery_error) > 1000:
            raise ValidationError("delivery error cannot exceed 1000 characters")
        object.__setattr__(self, "id", _identifier(self.id, "alert id"))
        object.__setattr__(self, "rule_id", _identifier(self.rule_id, "rule id"))
        object.__setattr__(self, "symbol", _symbol(self.symbol))
        object.__setattr__(self, "metric", self.metric.strip().lower())
        object.__setattr__(
            self, "observed_value", as_decimal(self.observed_value, "observed value")
        )
        object.__setattr__(self, "threshold", as_decimal(self.threshold, "threshold"))
        object.__setattr__(self, "operator", AlertOperator.parse(self.operator))
        object.__setattr__(self, "triggered_at", triggered_at)
        object.__setattr__(self, "message", message)
        object.__setattr__(self, "delivered_at", delivered_at)
        object.__setattr__(self, "acknowledged_at", acknowledged_at)

    def as_dict(self) -> Mapping[str, object]:
        return {
            "id": self.id,
            "ruleId": self.rule_id,
            "symbol": self.symbol,
            "metric": self.metric,
            "observedValue": str(self.observed_value),
            "threshold": str(self.threshold),
            "operator": self.operator.value,
            "triggeredAt": format_timestamp(self.triggered_at),
            "message": self.message,
            "status": self.status.value,
            "deliveredAt": format_timestamp(self.delivered_at) if self.delivered_at else None,
            "acknowledgedAt": (
                format_timestamp(self.acknowledged_at) if self.acknowledged_at else None
            ),
            "deliveryError": self.delivery_error,
        }


@dataclass(frozen=True)
class IngestionRun:
    """Auditable record for one provider fetch and persistence attempt."""

    id: str
    provider: str
    started_at: datetime
    status: RunStatus = RunStatus.RUNNING
    finished_at: Optional[datetime] = None
    requested_symbols: Tuple[str, ...] = ()
    imported_quotes: int = 0
    duplicate_quotes: int = 0
    rejected_quotes: int = 0
    error_code: Optional[str] = None
    error_message: Optional[str] = None

    def __post_init__(self) -> None:
        provider = self.provider.strip().lower() if isinstance(self.provider, str) else ""
        if not provider:
            raise ValidationError("run provider cannot be blank")
        started_at = ensure_utc(self.started_at, "started at")
        finished_at = ensure_utc(self.finished_at, "finished at") if self.finished_at else None
        if finished_at and finished_at < started_at:
            raise ValidationError("finished at cannot precede started at")
        symbols = tuple(dict.fromkeys(_symbol(symbol) for symbol in self.requested_symbols))
        for field_name, count in (
            ("imported quotes", self.imported_quotes),
            ("duplicate quotes", self.duplicate_quotes),
            ("rejected quotes", self.rejected_quotes),
        ):
            if count < 0:
                raise ValidationError(f"{field_name} cannot be negative")
        object.__setattr__(self, "id", _identifier(self.id, "run id"))
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "started_at", started_at)
        object.__setattr__(self, "finished_at", finished_at)
        object.__setattr__(self, "requested_symbols", symbols)

    @property
    def duration_seconds(self) -> Optional[float]:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()

    def finish(
        self,
        *,
        status: RunStatus,
        finished_at: datetime,
        imported_quotes: int = 0,
        duplicate_quotes: int = 0,
        rejected_quotes: int = 0,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> IngestionRun:
        if status is RunStatus.RUNNING:
            raise ValidationError("a finished run cannot remain running")
        return replace(
            self,
            status=status,
            finished_at=finished_at,
            imported_quotes=imported_quotes,
            duplicate_quotes=duplicate_quotes,
            rejected_quotes=rejected_quotes,
            error_code=error_code,
            error_message=error_message,
        )

    def as_dict(self) -> Mapping[str, object]:
        return {
            "id": self.id,
            "provider": self.provider,
            "status": self.status.value,
            "startedAt": format_timestamp(self.started_at),
            "finishedAt": format_timestamp(self.finished_at) if self.finished_at else None,
            "durationSeconds": self.duration_seconds,
            "requestedSymbols": list(self.requested_symbols),
            "importedQuotes": self.imported_quotes,
            "duplicateQuotes": self.duplicate_quotes,
            "rejectedQuotes": self.rejected_quotes,
            "errorCode": self.error_code,
            "errorMessage": self.error_message,
        }


def validate_quote_sequence(quotes: Iterable[Quote]) -> Tuple[Quote, ...]:
    """Return quotes in chronological order and reject duplicate observations."""

    result = tuple(sorted(quotes, key=lambda quote: (quote.observed_at, quote.symbol)))
    seen = set()
    for quote in result:
        key = (quote.provider, quote.symbol, quote.observed_at, quote.source_id)
        if key in seen:
            raise ValidationError("quote sequence contains duplicate observations")
        seen.add(key)
    return result


def common_symbol(quotes: Sequence[Quote]) -> str:
    """Return the common symbol for a non-empty homogeneous quote sequence."""

    if not quotes:
        raise ValidationError("quote sequence cannot be empty")
    symbol = quotes[0].symbol
    if any(quote.symbol != symbol for quote in quotes[1:]):
        raise ValidationError("quote sequence must contain one symbol")
    return symbol
