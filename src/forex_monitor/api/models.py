"""Transport-agnostic API request, response, identity, and delivery records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Optional, Tuple

from forex_monitor.errors import ValidationError


def text(value: object, name: str, maximum: Optional[int] = None) -> str:
    result = str(value).strip() if value is not None else ""
    if not result:
        raise ValidationError("%s is required" % name)
    if maximum is not None and len(result) > maximum:
        raise ValidationError("%s exceeds maximum length" % name)
    return result


def utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError("%s must be timezone-aware" % name)
    return value.astimezone(timezone.utc)


class HttpMethod(str, Enum):
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    PATCH = "PATCH"
    DELETE = "DELETE"


class DeliveryState(str, Enum):
    PENDING = "pending"
    DELIVERING = "delivering"
    DELIVERED = "delivered"
    RETRY = "retry"
    DEAD = "dead"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class ApiIdentity:
    principal_id: str
    client_id: str
    scopes: Tuple[str, ...]
    roles: Tuple[str, ...] = ()
    key_id: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "principal_id", text(self.principal_id, "principal id"))
        object.__setattr__(self, "client_id", text(self.client_id, "client id"))
        object.__setattr__(self, "scopes", tuple(sorted(set(item.strip().lower() for item in self.scopes if item.strip()))))
        object.__setattr__(self, "roles", tuple(sorted(set(item.strip().lower() for item in self.roles if item.strip()))))


@dataclass(frozen=True)
class ApiRequest:
    request_id: str
    method: HttpMethod
    path: str
    headers: Tuple[Tuple[str, str], ...] = ()
    query: Tuple[Tuple[str, str], ...] = ()
    body: Optional[Any] = None
    identity: Optional[ApiIdentity] = None
    received_at: datetime = datetime.min.replace(tzinfo=timezone.utc)
    remote_address: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", text(self.request_id, "request id"))
        object.__setattr__(self, "method", HttpMethod(self.method))
        path = text(self.path, "request path")
        if not path.startswith("/") or "?" in path:
            raise ValidationError("request path must be absolute and exclude query")
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "headers", tuple((key.lower(), value) for key, value in self.headers))
        object.__setattr__(self, "received_at", utc(self.received_at, "received at"))

    def header(self, name: str) -> Optional[str]:
        lowered = name.lower()
        values = [value for key, value in self.headers if key == lowered]
        return values[-1] if values else None

    def query_values(self, name: str) -> Tuple[str, ...]:
        return tuple(value for key, value in self.query if key == name)


@dataclass(frozen=True)
class ApiResponse:
    status: int
    body: Any = None
    headers: Tuple[Tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.status < 100 or self.status > 599:
            raise ValidationError("HTTP status is invalid")
        object.__setattr__(self, "headers", tuple((key.lower(), value) for key, value in self.headers))

    def with_header(self, name: str, value: object) -> "ApiResponse":
        lowered = name.lower()
        remaining = tuple(item for item in self.headers if item[0] != lowered)
        return ApiResponse(self.status, self.body, remaining + ((lowered, str(value)),))


@dataclass(frozen=True)
class ApiKey:
    key_id: str
    client_id: str
    principal_id: str
    secret_hash: str
    scopes: Tuple[str, ...]
    created_at: datetime
    expires_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    description: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "key_id", text(self.key_id, "key id"))
        object.__setattr__(self, "client_id", text(self.client_id, "client id"))
        object.__setattr__(self, "principal_id", text(self.principal_id, "principal id"))
        digest = text(self.secret_hash, "secret hash").lower()
        if len(digest) != 64 or any(item not in "0123456789abcdef" for item in digest):
            raise ValidationError("secret hash must be SHA-256")
        object.__setattr__(self, "secret_hash", digest)
        scopes = tuple(sorted(set(item.strip().lower() for item in self.scopes if item.strip())))
        if not scopes:
            raise ValidationError("API key requires scopes")
        object.__setattr__(self, "scopes", scopes)
        created = utc(self.created_at, "created at")
        object.__setattr__(self, "created_at", created)
        if self.expires_at is not None:
            expiry = utc(self.expires_at, "expires at")
            if expiry <= created:
                raise ValidationError("API key expiry must follow creation")
            object.__setattr__(self, "expires_at", expiry)
        if self.revoked_at is not None:
            object.__setattr__(self, "revoked_at", utc(self.revoked_at, "revoked at"))


@dataclass(frozen=True)
class WebhookSubscription:
    subscription_id: str
    client_id: str
    endpoint: str
    event_types: Tuple[str, ...]
    secret: str
    created_at: datetime
    active: bool = True
    maximum_attempts: int = 8
    timeout_seconds: int = 10

    def __post_init__(self) -> None:
        object.__setattr__(self, "subscription_id", text(self.subscription_id, "subscription id"))
        object.__setattr__(self, "client_id", text(self.client_id, "client id"))
        endpoint = text(self.endpoint, "webhook endpoint", 1000)
        if not endpoint.startswith("https://"):
            raise ValidationError("webhook endpoint must use HTTPS")
        object.__setattr__(self, "endpoint", endpoint)
        event_types = tuple(sorted(set(item.strip().lower() for item in self.event_types if item.strip())))
        if not event_types:
            raise ValidationError("webhook subscription requires event types")
        object.__setattr__(self, "event_types", event_types)
        object.__setattr__(self, "secret", text(self.secret, "webhook secret"))
        object.__setattr__(self, "created_at", utc(self.created_at, "created at"))
        if self.maximum_attempts < 1 or self.maximum_attempts > 100:
            raise ValidationError("webhook maximum attempts are invalid")
        if self.timeout_seconds < 1 or self.timeout_seconds > 120:
            raise ValidationError("webhook timeout is invalid")


@dataclass(frozen=True)
class WebhookDelivery:
    delivery_id: str
    subscription_id: str
    event_id: str
    event_type: str
    payload: bytes
    state: DeliveryState
    created_at: datetime
    next_attempt_at: datetime
    attempt_count: int = 0
    response_status: Optional[int] = None
    response_body: Optional[str] = None
    completed_at: Optional[datetime] = None
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "delivery_id", text(self.delivery_id, "delivery id"))
        object.__setattr__(self, "subscription_id", text(self.subscription_id, "subscription id"))
        object.__setattr__(self, "event_id", text(self.event_id, "event id"))
        object.__setattr__(self, "event_type", text(self.event_type, "event type").lower())
        if not isinstance(self.payload, bytes):
            raise ValidationError("webhook payload must be bytes")
        object.__setattr__(self, "state", DeliveryState(self.state))
        object.__setattr__(self, "created_at", utc(self.created_at, "created at"))
        object.__setattr__(self, "next_attempt_at", utc(self.next_attempt_at, "next attempt at"))
        if self.attempt_count < 0:
            raise ValidationError("webhook attempt count cannot be negative")
