"""Preference-aware message creation and provider delivery lifecycle."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Dict, Mapping, Optional, Tuple

from forex_monitor.communications.models import Channel, MessageDelivery, MessageState, text, utc
from forex_monitor.communications.preferences import PreferenceService
from forex_monitor.communications.templates import TemplateRegistry
from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError


class MessageDeliveryService:
    def __init__(self, preferences: PreferenceService, templates: TemplateRegistry) -> None:
        self.preferences = preferences
        self.templates = templates
        self._deliveries: Dict[str, MessageDelivery] = {}
        self._lock = RLock()

    def create(
        self,
        *,
        delivery_id: str,
        client_id: str,
        topic: str,
        variables: Mapping[str, object],
        created_at: datetime,
        channel: Optional[Channel] = None,
        campaign_id: Optional[str] = None,
        mandatory: bool = False,
    ) -> MessageDelivery:
        instant = utc(created_at, "created at")
        with self._lock:
            existing = self._deliveries.get(delivery_id)
            if existing is not None:
                return existing
        preference = self.preferences.preference(client_id, topic)
        if preference is None and not mandatory:
            raise ValidationError("client has no communication preference for topic")
        channels = preference.enabled_channels if preference else ((channel or Channel.IN_APP),)
        selected = Channel(channel) if channel is not None else channels[0] if channels else Channel.IN_APP
        if not mandatory and selected not in channels:
            raise ValidationError("requested channel is disabled for topic")
        contacts = self.preferences.contacts(client_id, selected)
        if not contacts:
            raise ValidationError("client has no verified contact for channel")
        locale = preference.locale if preference else "en"
        message = self.templates.render(topic, selected, locale, variables)
        quiet = preference is not None and not preference.mandatory and self.preferences.quiet(preference, instant)
        scheduled = self._end_of_quiet(preference, instant) if quiet else instant
        delivery = MessageDelivery(delivery_id, client_id, topic, selected, contacts[0].contact_id, message, MessageState.SCHEDULED if quiet else MessageState.PENDING, instant, scheduled, campaign_id)
        with self._lock:
            self._deliveries[delivery.delivery_id] = delivery
            return delivery

    def due(self, at: datetime, limit: int = 100) -> Tuple[MessageDelivery, ...]:
        instant = utc(at, "delivery time")
        with self._lock:
            values = [item for item in self._deliveries.values() if item.state in {MessageState.PENDING, MessageState.SCHEDULED, MessageState.FAILED} and item.scheduled_at <= instant]
            return tuple(sorted(values, key=lambda item: (item.scheduled_at, item.delivery_id))[:limit])

    def claim(self, delivery_id: str, *, at: datetime, expected_version: int) -> MessageDelivery:
        instant = utc(at, "claim time")
        with self._lock:
            current = self._require(delivery_id)
            if current.version != expected_version:
                raise StorageConflictError("message delivery version does not match")
            if current.state not in {MessageState.PENDING, MessageState.SCHEDULED, MessageState.FAILED} or current.scheduled_at > instant:
                raise ValidationError("message delivery is not claimable")
            changed = replace(current, state=MessageState.DELIVERING, attempt_count=current.attempt_count + 1, version=current.version + 1)
            self._deliveries[current.delivery_id] = changed
            return changed

    def record_result(self, delivery_id: str, *, delivered: bool, provider_reference: str, response: str, at: datetime, expected_version: int, retryable: bool = True) -> MessageDelivery:
        instant = utc(at, "result time")
        with self._lock:
            current = self._require(delivery_id)
            if current.version != expected_version:
                raise StorageConflictError("message delivery version does not match")
            if current.state != MessageState.DELIVERING:
                raise ValidationError("message is not being delivered")
            if delivered:
                changed = replace(current, state=MessageState.DELIVERED, provider_reference=provider_reference, response=response[:2000], completed_at=instant, version=current.version + 1)
            elif retryable and current.attempt_count < 8:
                changed = replace(current, state=MessageState.FAILED, provider_reference=provider_reference or None, response=response[:2000], scheduled_at=instant + timedelta(seconds=2 ** current.attempt_count), version=current.version + 1)
            else:
                changed = replace(current, state=MessageState.DEAD, provider_reference=provider_reference or None, response=response[:2000], completed_at=instant, version=current.version + 1)
            self._deliveries[current.delivery_id] = changed
            return changed

    def deliveries(self, *, client_id: Optional[str] = None, campaign_id: Optional[str] = None, state: Optional[MessageState] = None) -> Tuple[MessageDelivery, ...]:
        with self._lock:
            values = [item for item in self._deliveries.values() if (client_id is None or item.client_id == client_id) and (campaign_id is None or item.campaign_id == campaign_id) and (state is None or item.state == state)]
            return tuple(sorted(values, key=lambda item: (item.created_at, item.delivery_id)))

    def _require(self, delivery_id: str) -> MessageDelivery:
        value = self._deliveries.get(text(delivery_id, "delivery id"))
        if value is None:
            raise NotFoundError("message delivery does not exist")
        return value

    @staticmethod
    def _end_of_quiet(preference, at: datetime) -> datetime:
        from datetime import datetime as dt, timedelta as td
        from zoneinfo import ZoneInfo
        zone = ZoneInfo(preference.timezone_name)
        local = at.astimezone(zone)
        candidate = dt.combine(local.date(), preference.quiet_end, zone)
        if candidate <= local:
            candidate += td(days=1)
        return candidate.astimezone(timezone.utc)
