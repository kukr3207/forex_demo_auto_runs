"""Dependency-free JSON HTTP provider with bounded retry behavior."""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping, Optional, Sequence, Tuple

from forex_monitor.config import ProviderConfig
from forex_monitor.errors import (
    ProviderAuthenticationError,
    ProviderError,
    ProviderRateLimitError,
    ProviderResponseError,
)
from forex_monitor.models import Quote
from forex_monitor.providers.base import (
    Clock,
    HttpTransport,
    MarketDataProvider,
    ProviderHealth,
    RawProviderResponse,
    Sleeper,
    TransportRequest,
    TransportResponse,
    exponential_backoff,
    normalize_symbols,
    parse_retry_after,
)
from forex_monitor.providers.normalization import ProviderNormalizer


class UrllibTransport:
    """Small urllib adapter isolated behind the HttpTransport protocol."""

    def send(self, request: TransportRequest) -> TransportResponse:
        query = urllib.parse.urlencode(request.query)
        url = request.url + (("&" if "?" in request.url else "?") + query if query else "")
        http_request = urllib.request.Request(
            url=url,
            method=request.method,
            headers=dict(request.headers),
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(http_request, timeout=request.timeout_seconds) as response:
                body = response.read()
                status = int(response.status)
                headers = dict(response.headers.items())
        except urllib.error.HTTPError as error:
            body = error.read()
            status = int(error.code)
            headers = dict(error.headers.items()) if error.headers else {}
        except (urllib.error.URLError, socket.timeout, TimeoutError) as error:
            reason = getattr(error, "reason", error)
            if isinstance(reason, socket.timeout):
                raise TimeoutError("provider request timed out") from error
            raise ConnectionError(f"provider connection failed: {reason}") from error
        return TransportResponse(
            status=status,
            headers=headers,
            body=body,
            elapsed_seconds=time.monotonic() - started,
        )


@dataclass
class HttpJsonProvider(MarketDataProvider):
    """Fetch a JSON payload and delegate schema conversion to a normalizer."""

    config: ProviderConfig
    normalizer: ProviderNormalizer
    transport: HttpTransport = field(default_factory=UrllibTransport)
    clock: Clock = field(default=lambda: datetime.now(timezone.utc))
    sleep: Sleeper = time.sleep
    extra_headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.config.name

    def fetch_quotes(self, symbols: Sequence[str]) -> Tuple[Quote, ...]:
        requested = normalize_symbols(symbols)
        response = self._request(requested)
        quotes = self.normalizer.normalize(response, requested)
        return tuple(quote for quote in quotes if quote.symbol in requested)

    def health(self) -> ProviderHealth:
        checked_at = self.clock()
        started = time.monotonic()
        try:
            self.fetch_quotes(("EURUSD",))
        except ProviderError as error:
            return ProviderHealth(
                provider=self.name,
                healthy=False,
                checked_at=checked_at,
                latency_seconds=time.monotonic() - started,
                message=str(error),
            )
        return ProviderHealth(
            provider=self.name,
            healthy=True,
            checked_at=checked_at,
            latency_seconds=time.monotonic() - started,
        )

    def _request(self, symbols: Sequence[str]) -> RawProviderResponse:
        requested_at = self.clock()
        last_error: Optional[BaseException] = None
        elapsed = 0.0
        for attempt in range(1, self.config.max_attempts + 1):
            try:
                response = self.transport.send(self._transport_request(symbols))
            except (ConnectionError, TimeoutError, OSError) as error:
                last_error = error
                if attempt >= self.config.max_attempts:
                    raise ProviderError(
                        f"provider request failed after {attempt} attempts",
                        details={"provider": self.name, "attempts": attempt},
                    ) from error
                self.sleep(exponential_backoff(self.config.initial_backoff_seconds, attempt))
                continue
            elapsed += response.elapsed_seconds
            if response.status in {401, 403}:
                raise ProviderAuthenticationError(
                    "provider rejected configured credentials",
                    details={"provider": self.name, "status": response.status},
                )
            if response.status == 429:
                retry_after = parse_retry_after(response.headers.get("retry-after"))
                if attempt >= self.config.max_attempts:
                    raise ProviderRateLimitError(
                        "provider rate limit exhausted",
                        retry_after_seconds=retry_after,
                        details={"provider": self.name, "attempts": attempt},
                    )
                delay = (
                    retry_after
                    if retry_after is not None
                    else exponential_backoff(self.config.initial_backoff_seconds, attempt)
                )
                self.sleep(delay)
                continue
            if response.status >= 500:
                if attempt >= self.config.max_attempts:
                    raise ProviderError(
                        "provider returned a server error",
                        details={"provider": self.name, "status": response.status},
                    )
                self.sleep(exponential_backoff(self.config.initial_backoff_seconds, attempt))
                continue
            if response.status < 200 or response.status >= 300:
                raise ProviderResponseError(
                    "provider returned an unexpected status",
                    details={"provider": self.name, "status": response.status},
                )
            payload = self._decode_json(response)
            return RawProviderResponse(
                provider=self.name,
                requested_at=requested_at,
                received_at=self.clock(),
                status=response.status,
                payload=payload,
                headers=response.headers,
                attempts=attempt,
                elapsed_seconds=elapsed,
            )
        raise ProviderError("provider request failed", details={"cause": str(last_error)})

    def _transport_request(self, symbols: Sequence[str]) -> TransportRequest:
        headers = {
            "Accept": "application/json",
            "User-Agent": self.config.user_agent,
            **dict(self.extra_headers),
        }
        if self.config.api_key:
            headers.setdefault("Authorization", f"Bearer {self.config.api_key}")
        return TransportRequest(
            method="GET",
            url=self.config.base_url,
            headers=headers,
            query={"pairs": ",".join(symbols)},
            timeout_seconds=self.config.timeout_seconds,
        )

    def _decode_json(self, response: TransportResponse) -> object:
        if response.content_type not in {None, "application/json", "text/json"}:
            raise ProviderResponseError(
                "provider response is not JSON",
                details={"contentType": response.content_type},
            )
        try:
            text = response.text
        except (LookupError, UnicodeDecodeError) as error:
            raise ProviderResponseError("provider response is not valid text") from error
        if not text.strip():
            raise ProviderResponseError("provider returned an empty response")
        try:
            return json.loads(text)
        except json.JSONDecodeError as error:
            raise ProviderResponseError(
                "provider returned malformed JSON",
                details={"line": error.lineno, "column": error.colno},
            ) from error
