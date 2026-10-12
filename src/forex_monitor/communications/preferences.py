"""Contact verification and per-topic client channel preferences."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from threading import RLock
from typing import Dict, Optional, Tuple
from zoneinfo import ZoneInfo

from forex_monitor.communications.models import Channel, CommunicationPreference, ContactPoint
from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError


class PreferenceService:
    def __init__(self) -> None:
        self._contacts: Dict[str, ContactPoint] = {}
        self._client_contacts: Dict[str, list[str]] = {}
        self._preferences: Dict[Tuple[str, str], CommunicationPreference] = {}
        self._lock = RLock()

    def add_contact(self, contact: ContactPoint) -> ContactPoint:
        with self._lock:
            existing = self._contacts.get(contact.contact_id)
            if existing is not None:
                if existing == contact:
                    return existing
                raise StorageConflictError("contact id already exists")
            if contact.primary and any(self._contacts[item].channel == contact.channel and self._contacts[item].primary and self._contacts[item].active for item in self._client_contacts.get(contact.client_id, ())):
                raise StorageConflictError("client already has a primary contact for channel")
            self._contacts[contact.contact_id] = contact
            self._client_contacts.setdefault(contact.client_id, []).append(contact.contact_id)
            return contact

    def verify_contact(self, contact_id: str, *, verified_at: datetime) -> ContactPoint:
        with self._lock:
            current = self._contacts.get(contact_id.strip())
            if current is None:
                raise NotFoundError("contact does not exist")
            if current.verified_at is not None:
                return current
            changed = replace(current, verified_at=verified_at)
            self._contacts[current.contact_id] = changed
            return changed

    def set_preference(self, preference: CommunicationPreference, *, expected_version: Optional[int] = None) -> CommunicationPreference:
        key = (preference.client_id, preference.topic)
        with self._lock:
            current = self._preferences.get(key)
            if current is not None and expected_version is None:
                raise StorageConflictError("preference already exists")
            if current is not None and current.version != expected_version:
                raise StorageConflictError("preference version does not match")
            value = replace(preference, version=current.version + 1) if current else preference
            self._preferences[key] = value
            return value

    def preference(self, client_id: str, topic: str) -> Optional[CommunicationPreference]:
        with self._lock:
            return self._preferences.get((client_id, topic.lower()))

    def contacts(self, client_id: str, channel: Optional[Channel] = None, *, verified_only: bool = True) -> Tuple[ContactPoint, ...]:
        with self._lock:
            values = [self._contacts[item] for item in self._client_contacts.get(client_id, ())]
            values = [item for item in values if item.active and (channel is None or item.channel == channel) and (not verified_only or item.verified_at is not None)]
            return tuple(sorted(values, key=lambda item: (not item.primary, item.contact_id)))

    @staticmethod
    def quiet(preference: CommunicationPreference, at: datetime) -> bool:
        if preference.quiet_start is None or preference.quiet_end is None:
            return False
        local = at.astimezone(ZoneInfo(preference.timezone_name)).timetz().replace(tzinfo=None)
        if preference.quiet_start < preference.quiet_end:
            return preference.quiet_start <= local < preference.quiet_end
        return local >= preference.quiet_start or local < preference.quiet_end
