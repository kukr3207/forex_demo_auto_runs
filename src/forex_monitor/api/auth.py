"""Hashed API keys, rotation, revocation, and scope authorization."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import replace
from datetime import datetime, timedelta
from threading import RLock
from typing import Dict, Iterable, Optional, Tuple

from forex_monitor.api.models import ApiIdentity, ApiKey, text, utc
from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError


class ApiKeyService:
    def __init__(self, *, pepper: str) -> None:
        self._pepper = text(pepper, "API key pepper").encode("utf-8")
        self._keys: Dict[str, ApiKey] = {}
        self._client_keys: Dict[str, list[str]] = {}
        self._lock = RLock()

    def issue(
        self,
        *,
        key_id: str,
        client_id: str,
        principal_id: str,
        scopes: Iterable[str],
        created_at: datetime,
        ttl: Optional[timedelta] = None,
        description: str = "",
    ) -> Tuple[ApiKey, str]:
        instant = utc(created_at, "created at")
        if ttl is not None and ttl <= timedelta(0):
            raise ValidationError("API key TTL must be positive")
        secret = secrets.token_urlsafe(32)
        digest = self._digest(secret)
        key = ApiKey(key_id, client_id, principal_id, digest, tuple(scopes), instant, instant + ttl if ttl else None, description=description)
        with self._lock:
            if key.key_id in self._keys:
                raise StorageConflictError("API key id already exists")
            self._keys[key.key_id] = key
            self._client_keys.setdefault(key.client_id, []).append(key.key_id)
        return key, "%s.%s" % (key.key_id, secret)

    def authenticate(self, credential: str, *, at: datetime, required_scopes: Iterable[str] = ()) -> ApiIdentity:
        instant = utc(at, "authentication time")
        try:
            key_id, secret = credential.strip().split(".", 1)
        except ValueError as exc:
            raise ValidationError("API credential format is invalid") from exc
        with self._lock:
            key = self._keys.get(key_id)
            if key is None:
                raise ValidationError("API credential is invalid")
            if key.revoked_at is not None or (key.expires_at is not None and instant >= key.expires_at):
                raise ValidationError("API credential is inactive")
            if not hmac.compare_digest(key.secret_hash, self._digest(secret)):
                raise ValidationError("API credential is invalid")
            missing = set(item.lower() for item in required_scopes) - set(key.scopes)
            if missing:
                raise ValidationError("API credential lacks required scope")
            return ApiIdentity(key.principal_id, key.client_id, key.scopes, key_id=key.key_id)

    def revoke(self, key_id: str, *, revoked_at: datetime) -> ApiKey:
        instant = utc(revoked_at, "revoked at")
        with self._lock:
            current = self._keys.get(text(key_id, "key id"))
            if current is None:
                raise NotFoundError("API key does not exist")
            if current.revoked_at is not None:
                return current
            changed = replace(current, revoked_at=instant)
            self._keys[current.key_id] = changed
            return changed

    def active_keys(self, client_id: str, *, at: datetime) -> Tuple[ApiKey, ...]:
        instant = utc(at, "key time")
        with self._lock:
            values = [self._keys[item] for item in self._client_keys.get(client_id, ())]
            values = [item for item in values if item.revoked_at is None and (item.expires_at is None or instant < item.expires_at)]
            return tuple(sorted(values, key=lambda item: item.created_at))

    def _digest(self, secret: str) -> str:
        return hashlib.sha256(self._pepper + secret.encode("utf-8")).hexdigest()
