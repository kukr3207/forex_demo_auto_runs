from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from forex_monitor.api.auth import ApiKeyService
from forex_monitor.api.idempotency import IdempotencyStore
from forex_monitor.api.models import ApiIdentity, ApiRequest, ApiResponse, DeliveryState, HttpMethod, WebhookSubscription
from forex_monitor.api.pagination import CursorPaginator
from forex_monitor.api.ratelimit import RateLimitPolicy, RateLimiter
from forex_monitor.api.router import ApiRouter
from forex_monitor.api.webhooks import WebhookService
from forex_monitor.errors import StorageConflictError, ValidationError


NOW = datetime(2026, 10, 12, 8, tzinfo=timezone.utc)


def test_api_key_issue_authenticate_scope_and_revoke() -> None:
    service = ApiKeyService(pepper="pepper")
    key, credential = service.issue(key_id="key", client_id="client", principal_id="user", scopes=("quotes:read", "trades:write"), created_at=NOW, ttl=timedelta(days=1))
    identity = service.authenticate(credential, at=NOW, required_scopes=("quotes:read",))
    assert identity.key_id == key.key_id
    service.revoke(key.key_id, revoked_at=NOW)
    with pytest.raises(ValidationError):
        service.authenticate(credential, at=NOW)


def test_idempotency_replays_response_and_rejects_changed_request() -> None:
    store = IdempotencyStore()
    fingerprint = store.fingerprint("POST", "/orders", {"quantity": 1})
    assert store.begin("client", "key", fingerprint, at=NOW) is None
    response = ApiResponse(201, {"id": "order"})
    store.complete("client", "key", fingerprint, response, at=NOW)
    assert store.begin("client", "key", fingerprint, at=NOW) == response
    with pytest.raises(StorageConflictError):
        store.begin("client", "key", store.fingerprint("POST", "/orders", {"quantity": 2}), at=NOW)


def test_cursor_pagination_round_trips_and_rejects_tampering() -> None:
    paginator = CursorPaginator("secret", maximum_page_size=10)
    first = paginator.paginate(range(5), key=lambda item: (item,), limit=2, scope="numbers")
    assert first.items == (0, 1)
    second = paginator.paginate(range(5), key=lambda item: (item,), limit=2, cursor=first.next_cursor, scope="numbers")
    assert second.items == (2, 3)
    with pytest.raises(ValidationError):
        paginator.paginate(range(5), key=lambda item: (item,), limit=2, cursor=first.next_cursor[:-1] + "A", scope="numbers")


def test_rate_limiter_refills_token_bucket() -> None:
    limiter = RateLimiter()
    limiter.add_policy(RateLimitPolicy("standard", Decimal("2"), Decimal("1")))
    limiter.bind("quotes", "standard")
    assert limiter.check("client", "quotes", at=NOW).allowed
    assert limiter.check("client", "quotes", at=NOW).allowed
    denied = limiter.check("client", "quotes", at=NOW)
    assert not denied.allowed
    assert limiter.check("client", "quotes", at=NOW + timedelta(seconds=1)).allowed


def test_router_matches_parameters_enforces_scope_and_maps_errors() -> None:
    router = ApiRouter()
    router.add("quote", HttpMethod.GET, "/quotes/{quote_id}", lambda request, params: ApiResponse(200, {"id": params["quote_id"]}), required_scopes=("quotes:read",))
    unauthorized = router.dispatch(ApiRequest("one", HttpMethod.GET, "/quotes/q1", received_at=NOW))
    assert unauthorized.status == 401
    identity = ApiIdentity("user", "client", ("quotes:read",))
    response = router.dispatch(ApiRequest("two", HttpMethod.GET, "/quotes/q1", identity=identity, received_at=NOW))
    assert response.status == 200
    assert response.body == {"id": "q1"}
    assert dict(response.headers)["x-request-id"] == "two"


def test_webhook_publish_claim_sign_and_deliver() -> None:
    service = WebhookService()
    service.subscribe(WebhookSubscription("sub", "client", "https://example.test/hooks", ("trade.created",), "secret", NOW))
    delivery = service.publish(event_id="event", event_type="trade.created", payload={"tradeId": "one"}, created_at=NOW)[0]
    claimed, headers = service.claim(delivery.delivery_id, at=NOW, expected_version=1)
    assert headers["x-webhook-signature"].startswith("sha256=")
    complete = service.record_response(delivery.delivery_id, status=204, response_body="", at=NOW, expected_version=claimed.version)
    assert complete.state == DeliveryState.DELIVERED


def test_webhook_retries_transient_failure_and_dead_letters_permanent_failure() -> None:
    service = WebhookService()
    service.subscribe(WebhookSubscription("sub", "client", "https://example.test/hooks", ("trade.created",), "secret", NOW, maximum_attempts=2))
    delivery = service.publish(event_id="event", event_type="trade.created", payload={}, created_at=NOW)[0]
    claimed, _ = service.claim(delivery.delivery_id, at=NOW, expected_version=1)
    retry = service.record_response(delivery.delivery_id, status=500, response_body="error", at=NOW, expected_version=claimed.version)
    assert retry.state == DeliveryState.RETRY
    claimed, _ = service.claim(delivery.delivery_id, at=retry.next_attempt_at, expected_version=retry.version)
    dead = service.record_response(delivery.delivery_id, status=500, response_body="error", at=retry.next_attempt_at, expected_version=claimed.version)
    assert dead.state == DeliveryState.DEAD


def test_webhook_event_replay_does_not_duplicate_deliveries() -> None:
    service = WebhookService()
    service.subscribe(WebhookSubscription("sub", "client", "https://example.test/hooks", ("trade.created",), "secret", NOW))
    first = service.publish(event_id="event", event_type="trade.created", payload={"id": 1}, created_at=NOW)
    second = service.publish(event_id="event", event_type="trade.created", payload={"id": 1}, created_at=NOW)
    assert second == first
