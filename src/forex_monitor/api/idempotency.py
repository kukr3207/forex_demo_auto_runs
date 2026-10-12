"""Request fingerprints and response replay for safely retried mutations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Condition, RLock
from typing import Any, Dict, Optional, Tuple

from forex_monitor.api.models import ApiResponse, text, utc
from forex_monitor.errors import StorageConflictError, ValidationError


@dataclass(frozen=True)
class IdempotencyRecord:
    client_id: str
    key: str
    fingerprint: str
    created_at: datetime
    expires_at: datetime
    response: Optional[ApiResponse] = None
    completed_at: Optional[datetime] = None


class IdempotencyStore:
    def __init__(self, *, ttl: timedelta = timedelta(hours=24)) -> None:
        if ttl <= timedelta(0):
            raise ValidationError("idempotency TTL must be positive")
        self._ttl = ttl
        self._records: Dict[Tuple[str, str], IdempotencyRecord] = {}
        self._lock = RLock()
        self._condition = Condition(self._lock)

    @staticmethod
    def fingerprint(method: str, path: str, body: Any) -> str:
        payload = json.dumps({"method": method.upper(), "path": path, "body": body}, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def begin(self, client_id: str, key: str, fingerprint: str, *, at: datetime) -> Optional[ApiResponse]:
        instant = utc(at, "idempotency time")
        identity = (text(client_id, "client id"), text(key, "idempotency key"))
        with self._lock:
            current = self._records.get(identity)
            if current is not None and current.expires_at <= instant:
                self._records.pop(identity)
                current = None
            if current is None:
                self._records[identity] = IdempotencyRecord(identity[0], identity[1], fingerprint, instant, instant + self._ttl)
                return None
            if current.fingerprint != fingerprint:
                raise StorageConflictError("idempotency key was reused with another request")
            if current.response is None:
                raise StorageConflictError("matching idempotent request is still in progress")
            return current.response

    def complete(self, client_id: str, key: str, fingerprint: str, response: ApiResponse, *, at: datetime) -> ApiResponse:
        instant = utc(at, "completion time")
        identity = (text(client_id, "client id"), text(key, "idempotency key"))
        with self._condition:
            current = self._records.get(identity)
            if current is None or current.fingerprint != fingerprint:
                raise StorageConflictError("idempotency reservation does not match")
            if current.response is not None:
                if current.response == response:
                    return current.response
                raise StorageConflictError("idempotent request already has another response")
            changed = IdempotencyRecord(current.client_id, current.key, current.fingerprint, current.created_at, current.expires_at, response, instant)
            self._records[identity] = changed
            self._condition.notify_all()
            return response

    def abandon(self, client_id: str, key: str, fingerprint: str) -> bool:
        identity = (client_id.strip(), key.strip())
        with self._condition:
            current = self._records.get(identity)
            if current is None:
                return False
            if current.fingerprint != fingerprint or current.response is not None:
                raise StorageConflictError("idempotency reservation cannot be abandoned")
            self._records.pop(identity)
            self._condition.notify_all()
            return True

    def purge(self, *, at: datetime) -> int:
        instant = utc(at, "purge time")
        with self._lock:
            expired = [key for key, value in self._records.items() if value.expires_at <= instant]
            for key in expired:
                self._records.pop(key)
            return len(expired)
