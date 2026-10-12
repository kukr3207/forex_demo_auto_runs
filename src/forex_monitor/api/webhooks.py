"""Signed webhook fan-out with retries and dead-letter handling."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import replace
from datetime import datetime, timedelta
from threading import RLock
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from forex_monitor.api.models import DeliveryState, WebhookDelivery, WebhookSubscription, text, utc
from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError


class WebhookService:
    def __init__(self) -> None:
        self._subscriptions: Dict[str, WebhookSubscription] = {}
        self._deliveries: Dict[str, WebhookDelivery] = {}
        self._event_deliveries: Dict[str, list[str]] = {}
        self._lock = RLock()

    def subscribe(self, subscription: WebhookSubscription) -> WebhookSubscription:
        with self._lock:
            existing = self._subscriptions.get(subscription.subscription_id)
            if existing is not None:
                if existing == subscription:
                    return existing
                raise StorageConflictError("webhook subscription id already exists")
            self._subscriptions[subscription.subscription_id] = subscription
            return subscription

    def publish(self, *, event_id: str, event_type: str, payload: Mapping[str, Any], created_at: datetime) -> Tuple[WebhookDelivery, ...]:
        instant = utc(created_at, "created at")
        event_key = text(event_id, "event id")
        kind = text(event_type, "event type").lower()
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        with self._lock:
            existing_ids = self._event_deliveries.get(event_key)
            if existing_ids is not None:
                existing = tuple(self._deliveries[item] for item in existing_ids)
                if all(item.event_type == kind and item.payload == serialized for item in existing):
                    return existing
                raise StorageConflictError("webhook event id already has another payload")
            values = []
            for subscription in sorted(self._subscriptions.values(), key=lambda item: item.subscription_id):
                if not subscription.active or kind not in subscription.event_types:
                    continue
                delivery = WebhookDelivery("%s:%s" % (event_key, subscription.subscription_id), subscription.subscription_id, event_key, kind, serialized, DeliveryState.PENDING, instant, instant)
                self._deliveries[delivery.delivery_id] = delivery
                values.append(delivery)
            self._event_deliveries[event_key] = [item.delivery_id for item in values]
            return tuple(values)

    def due(self, at: datetime, *, limit: int = 100) -> Tuple[WebhookDelivery, ...]:
        instant = utc(at, "delivery time")
        if limit < 1:
            raise ValidationError("delivery limit must be positive")
        with self._lock:
            values = [item for item in self._deliveries.values() if item.state in {DeliveryState.PENDING, DeliveryState.RETRY} and item.next_attempt_at <= instant]
            return tuple(sorted(values, key=lambda item: (item.next_attempt_at, item.delivery_id))[:limit])

    def claim(self, delivery_id: str, *, at: datetime, expected_version: int) -> Tuple[WebhookDelivery, Mapping[str, str]]:
        instant = utc(at, "claim time")
        with self._lock:
            current = self._require_delivery(delivery_id)
            if current.version != expected_version:
                raise StorageConflictError("webhook delivery version does not match")
            if current.state not in {DeliveryState.PENDING, DeliveryState.RETRY} or current.next_attempt_at > instant:
                raise ValidationError("webhook delivery is not claimable")
            subscription = self._subscriptions[current.subscription_id]
            changed = replace(current, state=DeliveryState.DELIVERING, attempt_count=current.attempt_count + 1, version=current.version + 1)
            self._deliveries[current.delivery_id] = changed
            timestamp = str(int(instant.timestamp()))
            signature = hmac.new(subscription.secret.encode("utf-8"), timestamp.encode("ascii") + b"." + current.payload, hashlib.sha256).hexdigest()
            headers = {"content-type": "application/json", "x-webhook-id": current.delivery_id, "x-webhook-timestamp": timestamp, "x-webhook-signature": "sha256=%s" % signature}
            return changed, headers

    def record_response(self, delivery_id: str, *, status: int, response_body: str, at: datetime, expected_version: int) -> WebhookDelivery:
        instant = utc(at, "response time")
        with self._lock:
            current = self._require_delivery(delivery_id)
            if current.version != expected_version:
                raise StorageConflictError("webhook delivery version does not match")
            if current.state != DeliveryState.DELIVERING:
                raise ValidationError("webhook delivery is not in progress")
            subscription = self._subscriptions[current.subscription_id]
            body = response_body[:2000]
            if 200 <= status < 300:
                changed = replace(current, state=DeliveryState.DELIVERED, response_status=status, response_body=body, completed_at=instant, version=current.version + 1)
            elif current.attempt_count >= subscription.maximum_attempts or 400 <= status < 500 and status not in {408, 409, 425, 429}:
                changed = replace(current, state=DeliveryState.DEAD, response_status=status, response_body=body, completed_at=instant, version=current.version + 1)
            else:
                delay = min(timedelta(hours=6), timedelta(seconds=2 ** min(current.attempt_count, 15)))
                changed = replace(current, state=DeliveryState.RETRY, response_status=status, response_body=body, next_attempt_at=instant + delay, version=current.version + 1)
            self._deliveries[current.delivery_id] = changed
            return changed

    def cancel(self, delivery_id: str) -> WebhookDelivery:
        with self._lock:
            current = self._require_delivery(delivery_id)
            if current.state in {DeliveryState.DELIVERED, DeliveryState.DEAD, DeliveryState.CANCELLED}:
                return current
            changed = replace(current, state=DeliveryState.CANCELLED, version=current.version + 1)
            self._deliveries[current.delivery_id] = changed
            return changed

    def deliveries(self, *, subscription_id: Optional[str] = None, state: Optional[DeliveryState] = None) -> Tuple[WebhookDelivery, ...]:
        with self._lock:
            values = [item for item in self._deliveries.values() if (subscription_id is None or item.subscription_id == subscription_id) and (state is None or item.state == state)]
            return tuple(sorted(values, key=lambda item: (item.created_at, item.delivery_id)))

    def _require_delivery(self, delivery_id: str) -> WebhookDelivery:
        value = self._deliveries.get(text(delivery_id, "delivery id"))
        if value is None:
            raise NotFoundError("webhook delivery does not exist")
        return value
