"""Provider payload normalization with strict schema checks."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping, Optional, Protocol, Sequence, Tuple

from forex_monitor.errors import ProviderResponseError
from forex_monitor.models import Quote, parse_timestamp
from forex_monitor.providers.base import RawProviderResponse, normalize_symbols


class ProviderNormalizer(Protocol):
    """Convert a raw provider payload into canonical quotes."""

    def normalize(
        self,
        response: RawProviderResponse,
        symbols: Sequence[str],
    ) -> Tuple[Quote, ...]:
        ...


def canonical_json(value: Any) -> str:
    """Serialize a JSON value deterministically for checksums and source IDs."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def payload_checksum(value: Any) -> str:
    """Return a SHA-256 digest of canonical JSON."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ProviderResponseError(f"{field_name} must be an object")
    for key in value:
        if not isinstance(key, str):
            raise ProviderResponseError(f"{field_name} keys must be strings")
    return value


def _first(mapping: Mapping[str, object], names: Sequence[str]) -> object:
    for name in names:
        if name in mapping:
            return mapping[name]
    return None


def _price(value: object, field_name: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ProviderResponseError(f"{field_name} must be numeric")
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise ProviderResponseError(f"{field_name} must be numeric") from exc
    if not result.is_finite() or result <= 0:
        raise ProviderResponseError(f"{field_name} must be a finite positive number")
    return result


def _percentage(value: object, field_name: str) -> Decimal:
    if isinstance(value, str):
        value = value.strip().rstrip("%")
    if isinstance(value, bool) or value is None:
        raise ProviderResponseError(f"{field_name} must be numeric")
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise ProviderResponseError(f"{field_name} must be numeric") from exc
    if not result.is_finite() or result < 0 or result > 100:
        raise ProviderResponseError(f"{field_name} must be between 0 and 100")
    return result


@dataclass(frozen=True)
class FxssiNormalizer:
    """Normalize FXSSI current-ratio responses.

    The historical script persisted whatever JSON the endpoint returned. The
    endpoint has used more than one envelope and field spelling over time, so
    this adapter accepts known public representations but validates every row
    before creating a Quote.

    FXSSI sentiment data may not include a market bid/ask. When it does not,
    long and short percentages are encoded as a synthetic quote solely for
    compatibility with the original data stream: bid is short percentage and
    ask is long percentage, with the raw semantics declared in metadata.
    """

    provider_name: str = "fxssi"

    def normalize(
        self,
        response: RawProviderResponse,
        symbols: Sequence[str],
    ) -> Tuple[Quote, ...]:
        if response.provider != self.provider_name:
            raise ProviderResponseError("normalizer provider does not match response")
        requested = normalize_symbols(symbols)
        rows, envelope_timestamp = self._rows(response.payload)
        observed_at = envelope_timestamp or response.received_at
        normalized = []
        seen = set()
        for index, value in enumerate(rows):
            row = _mapping(value, f"payload row {index}")
            symbol_value = _first(row, ("symbol", "pair", "instrument", "name"))
            if not isinstance(symbol_value, str) or not symbol_value.strip():
                raise ProviderResponseError(f"payload row {index} is missing a symbol")
            symbol = symbol_value.strip().upper().replace("/", "")
            if symbol not in requested:
                continue
            if symbol in seen:
                raise ProviderResponseError(f"provider returned duplicate symbol {symbol}")
            row_timestamp = _first(row, ("timestamp", "observedAt", "updatedAt", "time"))
            quote_time = parse_timestamp(row_timestamp) if row_timestamp else observed_at
            quote = self._quote(row, symbol, quote_time, index)
            normalized.append(quote)
            seen.add(symbol)
        return tuple(normalized)

    def _rows(self, payload: object) -> Tuple[Sequence[object], Optional[datetime]]:
        envelope_timestamp = None
        if isinstance(payload, list):
            return payload, None
        envelope = _mapping(payload, "provider payload")
        timestamp = _first(envelope, ("timestamp", "observedAt", "updatedAt", "time"))
        if timestamp is not None:
            try:
                envelope_timestamp = parse_timestamp(timestamp)
            except Exception as exc:
                raise ProviderResponseError("provider timestamp is invalid") from exc
        candidate = _first(envelope, ("data", "results", "pairs", "symbols", "items"))
        if isinstance(candidate, list):
            return candidate, envelope_timestamp
        if isinstance(candidate, Mapping):
            rows = []
            for symbol, raw in candidate.items():
                if not isinstance(symbol, str):
                    raise ProviderResponseError("provider symbol keys must be strings")
                row = dict(_mapping(raw, f"provider symbol {symbol}"))
                row.setdefault("symbol", symbol)
                rows.append(row)
            return rows, envelope_timestamp
        reserved = {"status", "success", "timestamp", "observedAt", "updatedAt", "time"}
        symbol_rows = []
        for symbol, raw in envelope.items():
            if symbol in reserved:
                continue
            if isinstance(raw, Mapping):
                row = dict(raw)
                row.setdefault("symbol", symbol)
                symbol_rows.append(row)
        if symbol_rows:
            return symbol_rows, envelope_timestamp
        raise ProviderResponseError("provider payload does not contain quote rows")

    def _quote(
        self,
        row: Mapping[str, object],
        symbol: str,
        observed_at: datetime,
        index: int,
    ) -> Quote:
        bid_value = _first(row, ("bid", "buy", "bidPrice", "bid_price"))
        ask_value = _first(row, ("ask", "sell", "askPrice", "ask_price"))
        metadata = {}
        if bid_value is not None or ask_value is not None:
            if bid_value is None or ask_value is None:
                raise ProviderResponseError(f"payload row {index} must contain bid and ask")
            bid = _price(bid_value, f"payload row {index} bid")
            ask = _price(ask_value, f"payload row {index} ask")
            metadata["valueKind"] = "market_price"
        else:
            long_value = _first(
                row,
                ("long", "longs", "buyRatio", "buyers", "buy", "average"),
            )
            short_value = _first(row, ("short", "shorts", "sellRatio", "sellers", "sell"))
            if long_value is None:
                raise ProviderResponseError(
                    f"payload row {index} must contain prices or sentiment ratios"
                )
            long_ratio = _percentage(long_value, f"payload row {index} long ratio")
            short_ratio = (
                _percentage(short_value, f"payload row {index} short ratio")
                if short_value is not None
                else Decimal("100") - long_ratio
            )
            if abs((long_ratio + short_ratio) - Decimal("100")) > Decimal("1"):
                raise ProviderResponseError(
                    f"payload row {index} sentiment ratios must total approximately 100"
                )
            bid, ask = sorted((short_ratio, long_ratio))
            metadata.update(
                {
                    "valueKind": "sentiment_percentage",
                    "longPercentage": str(long_ratio),
                    "shortPercentage": str(short_ratio),
                }
            )
        volume_value = _first(row, ("volume", "tickVolume", "count"))
        source_material = {
            "symbol": symbol,
            "observedAt": observed_at.isoformat(),
            "row": row,
        }
        return Quote(
            symbol=symbol,
            bid=bid,
            ask=ask,
            observed_at=observed_at,
            provider=self.provider_name,
            source_id=payload_checksum(source_material)[:40],
            volume=_price(volume_value, "volume") if volume_value is not None else None,
            metadata=metadata,
        )


@dataclass(frozen=True)
class GenericQuoteNormalizer:
    """Strict normalizer for fixtures and conventional quote endpoints."""

    provider_name: str
    rows_key: str = "quotes"

    def normalize(
        self,
        response: RawProviderResponse,
        symbols: Sequence[str],
    ) -> Tuple[Quote, ...]:
        requested = set(normalize_symbols(symbols))
        envelope = _mapping(response.payload, "provider payload")
        rows = envelope.get(self.rows_key)
        if not isinstance(rows, list):
            raise ProviderResponseError(f"provider payload must contain {self.rows_key}")
        result = []
        seen = set()
        for index, raw in enumerate(rows):
            row = _mapping(raw, f"quote row {index}")
            symbol = str(row.get("symbol", "")).strip().upper().replace("/", "")
            if symbol not in requested:
                continue
            if symbol in seen:
                raise ProviderResponseError(f"provider returned duplicate symbol {symbol}")
            try:
                result.append(
                    Quote(
                        symbol=symbol,
                        bid=_price(row.get("bid"), f"quote row {index} bid"),
                        ask=_price(row.get("ask"), f"quote row {index} ask"),
                        observed_at=(
                            parse_timestamp(row["observedAt"])
                            if row.get("observedAt") is not None
                            else response.received_at
                        ),
                        provider=self.provider_name,
                        source_id=(
                            str(row["sourceId"])
                            if row.get("sourceId") is not None
                            else payload_checksum(row)[:40]
                        ),
                        volume=(
                            _price(row["volume"], f"quote row {index} volume")
                            if row.get("volume") is not None
                            else None
                        ),
                    )
                )
            except ProviderResponseError:
                raise
            except Exception as exc:
                raise ProviderResponseError(f"quote row {index} is invalid") from exc
            seen.add(symbol)
        return tuple(result)
