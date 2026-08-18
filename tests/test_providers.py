from __future__ import annotations

import json
import unittest
from collections import deque
from datetime import timedelta
from decimal import Decimal
from typing import Iterable, Optional, Sequence

from forex_monitor.config import ProviderConfig
from forex_monitor.errors import (
    ProviderAuthenticationError,
    ProviderError,
    ProviderRateLimitError,
    ProviderResponseError,
)
from forex_monitor.providers import FixtureProvider, FxssiNormalizer, HttpJsonProvider
from forex_monitor.providers.base import (
    RawProviderResponse,
    TransportRequest,
    TransportResponse,
    exponential_backoff,
    normalize_symbols,
    parse_retry_after,
)
from forex_monitor.providers.normalization import (
    GenericQuoteNormalizer,
    canonical_json,
    payload_checksum,
)
from tests.support import BASE_TIME, MutableClock, quote


class FakeTransport:
    def __init__(self, responses: Iterable[object]) -> None:
        self.responses = deque(responses)
        self.requests: list[TransportRequest] = []

    def send(self, request: TransportRequest) -> TransportResponse:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("fake transport was called too many times")
        response = self.responses.popleft()
        if isinstance(response, BaseException):
            raise response
        assert isinstance(response, TransportResponse)
        return response


def response(
    status: int = 200,
    payload: object = None,
    *,
    content_type: str = "application/json",
    headers: Optional[dict[str, str]] = None,
) -> TransportResponse:
    body = json.dumps({"quotes": []} if payload is None else payload).encode()
    return TransportResponse(
        status,
        {"content-type": content_type, **(headers or {})},
        body,
        0.05,
    )


def raw(payload: object, *, provider: str = "fxssi") -> RawProviderResponse:
    return RawProviderResponse(
        provider,
        BASE_TIME,
        BASE_TIME + timedelta(milliseconds=10),
        200,
        payload,
        {"content-type": "application/json"},
        1,
        0.01,
    )


class ProviderHelperTests(unittest.TestCase):
    def test_normalize_symbols_preserves_first_order(self) -> None:
        self.assertEqual(
            normalize_symbols(("eur/usd", "GBPUSD", " EURUSD ")),
            ("EURUSD", "GBPUSD"),
        )

    def test_normalize_symbols_requires_values(self) -> None:
        with self.assertRaises(ValueError):
            normalize_symbols(())
        with self.assertRaises(ValueError):
            normalize_symbols(("",))

    def test_exponential_backoff_is_bounded(self) -> None:
        self.assertEqual(exponential_backoff(0.5, 1), 0.5)
        self.assertEqual(exponential_backoff(0.5, 4), 4.0)
        self.assertEqual(exponential_backoff(10, 5, maximum_seconds=30), 30)

    def test_parse_retry_after(self) -> None:
        self.assertEqual(parse_retry_after(" 2.5 "), 2.5)
        self.assertEqual(parse_retry_after("-1"), 0)
        self.assertIsNone(parse_retry_after("tomorrow"))
        self.assertIsNone(parse_retry_after(None))

    def test_canonical_json_and_checksum_are_stable(self) -> None:
        left = {"b": 2, "a": [1, True]}
        right = {"a": [1, True], "b": 2}
        self.assertEqual(canonical_json(left), canonical_json(right))
        self.assertEqual(payload_checksum(left), payload_checksum(right))
        self.assertEqual(len(payload_checksum(left)), 64)


class GenericNormalizerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.normalizer = GenericQuoteNormalizer("demo")

    def test_normalizes_requested_rows(self) -> None:
        result = self.normalizer.normalize(
            raw(
                {
                    "quotes": [
                        {"symbol": "EUR/USD", "bid": "1.1", "ask": "1.2"},
                        {"symbol": "GBPUSD", "bid": "1.3", "ask": "1.4"},
                    ]
                },
                provider="demo",
            ),
            ("EURUSD",),
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].symbol, "EURUSD")
        self.assertEqual(result[0].provider, "demo")

    def test_missing_rows_key_is_rejected(self) -> None:
        with self.assertRaisesRegex(ProviderResponseError, "quotes"):
            self.normalizer.normalize(raw({}, provider="demo"), ("EURUSD",))

    def test_duplicate_symbol_is_rejected(self) -> None:
        payload = {
            "quotes": [
                {"symbol": "EURUSD", "bid": 1, "ask": 2},
                {"symbol": "EURUSD", "bid": 1, "ask": 2},
            ]
        }
        with self.assertRaisesRegex(ProviderResponseError, "duplicate"):
            self.normalizer.normalize(raw(payload, provider="demo"), ("EURUSD",))

    def test_bad_price_is_rejected(self) -> None:
        for value in (None, 0, -1, "bad", float("inf")):
            with self.subTest(value=value), self.assertRaises(ProviderResponseError):
                self.normalizer.normalize(
                    raw(
                        {"quotes": [{"symbol": "EURUSD", "bid": value, "ask": 2}]},
                        provider="demo",
                    ),
                    ("EURUSD",),
                )


class FxssiNormalizerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.normalizer = FxssiNormalizer()

    def test_list_of_market_quotes(self) -> None:
        result = self.normalizer.normalize(
            raw([{"pair": "EURUSD", "bid": 1.1, "ask": 1.2}]),
            ("EURUSD",),
        )
        self.assertEqual(result[0].metadata["valueKind"], "market_price")

    def test_pairs_mapping_sentiment_payload(self) -> None:
        result = self.normalizer.normalize(
            raw({"pairs": {"EURUSD": {"average": 61}}}),
            ("EURUSD",),
        )
        self.assertEqual(result[0].metadata["valueKind"], "sentiment_percentage")
        self.assertEqual(result[0].metadata["longPercentage"], "61")
        self.assertEqual(result[0].metadata["shortPercentage"], "39")

    def test_data_mapping_with_known_fields(self) -> None:
        result = self.normalizer.normalize(
            raw({"data": {"EURUSD": {"long": "55%", "short": "45%"}}}),
            ("EURUSD",),
        )
        self.assertEqual(result[0].bid, Decimal("45"))
        self.assertEqual(result[0].ask, Decimal("55"))

    def test_envelope_timestamp_is_used(self) -> None:
        result = self.normalizer.normalize(
            raw(
                {
                    "observedAt": "2026-02-01T10:00:00Z",
                    "data": [{"symbol": "EURUSD", "long": 50, "short": 50}],
                }
            ),
            ("EURUSD",),
        )
        self.assertEqual(result[0].observed_at.hour, 10)

    def test_unrequested_symbols_are_ignored(self) -> None:
        result = self.normalizer.normalize(
            raw([{"symbol": "GBPUSD", "long": 50, "short": 50}]),
            ("EURUSD",),
        )
        self.assertEqual(result, ())

    def test_bad_sentiment_total_is_rejected(self) -> None:
        with self.assertRaisesRegex(ProviderResponseError, "total"):
            self.normalizer.normalize(
                raw([{"symbol": "EURUSD", "long": 80, "short": 80}]),
                ("EURUSD",),
            )

    def test_missing_values_are_rejected(self) -> None:
        with self.assertRaises(ProviderResponseError):
            self.normalizer.normalize(raw([{"symbol": "EURUSD"}]), ("EURUSD",))

    def test_provider_mismatch_is_rejected(self) -> None:
        with self.assertRaises(ProviderResponseError):
            self.normalizer.normalize(raw([], provider="other"), ("EURUSD",))


class FixtureProviderTests(unittest.TestCase):
    def test_filters_and_reorders_batch(self) -> None:
        provider = FixtureProvider([(quote("GBPUSD"), quote("EURUSD", source_id="source_2"))])
        result = provider.fetch_quotes(("EURUSD", "GBPUSD"))
        self.assertEqual([item.symbol for item in result], ["EURUSD", "GBPUSD"])
        self.assertEqual(provider.calls, [("EURUSD", "GBPUSD")])

    def test_repeats_last_batch_by_default(self) -> None:
        provider = FixtureProvider([(quote(),)])
        self.assertEqual(provider.fetch_quotes(("EURUSD",)), provider.fetch_quotes(("EURUSD",)))

    def test_exhaustion_can_fail(self) -> None:
        provider = FixtureProvider([(quote(),)], repeat_last=False)
        provider.fetch_quotes(("EURUSD",))
        with self.assertRaises(ProviderError):
            provider.fetch_quotes(("EURUSD",))

    def test_queued_exception_is_raised(self) -> None:
        provider = FixtureProvider([ProviderError("boom")])
        with self.assertRaisesRegex(ProviderError, "boom"):
            provider.fetch_quotes(("EURUSD",))


class HttpProviderTests(unittest.TestCase):
    def make_provider(
        self,
        responses: Sequence[object],
        *,
        attempts: int = 3,
    ) -> tuple[HttpJsonProvider, FakeTransport, list[float]]:
        transport = FakeTransport(responses)
        sleeps: list[float] = []
        provider = HttpJsonProvider(
            ProviderConfig(
                name="demo", base_url="https://example.test/quotes", max_attempts=attempts
            ),
            GenericQuoteNormalizer("demo"),
            transport=transport,
            clock=MutableClock(),
            sleep=sleeps.append,
        )
        return provider, transport, sleeps

    def test_successful_request_contains_normalized_query(self) -> None:
        provider, transport, sleeps = self.make_provider(
            [response(payload={"quotes": [{"symbol": "EURUSD", "bid": 1, "ask": 2}]})]
        )
        result = provider.fetch_quotes(("eur/usd",))
        self.assertEqual(len(result), 1)
        self.assertEqual(transport.requests[0].query, {"pairs": "EURUSD"})
        self.assertEqual(sleeps, [])

    def test_connection_error_retries(self) -> None:
        provider, transport, sleeps = self.make_provider(
            [ConnectionError("offline"), response(payload={"quotes": []})]
        )
        self.assertEqual(provider.fetch_quotes(("EURUSD",)), ())
        self.assertEqual(len(transport.requests), 2)
        self.assertEqual(sleeps, [0.25])

    def test_server_error_retries(self) -> None:
        provider, _, sleeps = self.make_provider([response(503), response(payload={"quotes": []})])
        provider.fetch_quotes(("EURUSD",))
        self.assertEqual(sleeps, [0.25])

    def test_rate_limit_honors_retry_after(self) -> None:
        provider, _, sleeps = self.make_provider(
            [response(429, headers={"retry-after": "3"}), response(payload={"quotes": []})]
        )
        provider.fetch_quotes(("EURUSD",))
        self.assertEqual(sleeps, [3.0])

    def test_final_rate_limit_has_typed_error(self) -> None:
        provider, _, _ = self.make_provider(
            [response(429, headers={"retry-after": "2"})], attempts=1
        )
        with self.assertRaises(ProviderRateLimitError) as raised:
            provider.fetch_quotes(("EURUSD",))
        self.assertEqual(raised.exception.retry_after_seconds, 2)

    def test_authentication_error_is_not_retried(self) -> None:
        provider, transport, sleeps = self.make_provider([response(401)])
        with self.assertRaises(ProviderAuthenticationError):
            provider.fetch_quotes(("EURUSD",))
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(sleeps, [])

    def test_unexpected_client_error(self) -> None:
        provider, _, _ = self.make_provider([response(422)])
        with self.assertRaises(ProviderResponseError):
            provider.fetch_quotes(("EURUSD",))

    def test_invalid_json_is_rejected(self) -> None:
        malformed = TransportResponse(200, {"content-type": "application/json"}, b"{", 0.1)
        provider, _, _ = self.make_provider([malformed])
        with self.assertRaisesRegex(ProviderResponseError, "malformed"):
            provider.fetch_quotes(("EURUSD",))

    def test_wrong_content_type_is_rejected(self) -> None:
        provider, _, _ = self.make_provider([response(content_type="text/html")])
        with self.assertRaisesRegex(ProviderResponseError, "not JSON"):
            provider.fetch_quotes(("EURUSD",))


if __name__ == "__main__":
    unittest.main()
