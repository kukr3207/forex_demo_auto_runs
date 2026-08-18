"""Opaque identifier factories."""

from __future__ import annotations

import secrets
import uuid
from typing import Callable


def uuid_hex() -> str:
    """Return a lowercase 32-character UUID identifier."""

    return uuid.uuid4().hex


def token_id(prefix: str = "", *, bytes_count: int = 16) -> str:
    """Return a URL-safe random identifier with an optional validated prefix."""

    normalized = prefix.strip().lower()
    if normalized and (not normalized.isalnum() or len(normalized) > 12):
        raise ValueError("identifier prefix must be at most 12 alphanumeric characters")
    if bytes_count < 8 or bytes_count > 64:
        raise ValueError("identifier entropy must be between 8 and 64 bytes")
    token = secrets.token_hex(bytes_count)
    return f"{normalized}_{token}" if normalized else token


def deterministic_id(namespace: uuid.UUID, value: str, *, prefix: str = "") -> str:
    """Return a stable UUIDv5 identifier for an external idempotency key."""

    if not isinstance(namespace, uuid.UUID):
        raise TypeError("namespace must be a UUID")
    normalized = value.strip()
    if not normalized:
        raise ValueError("deterministic identifier input cannot be blank")
    identifier = uuid.uuid5(namespace, normalized).hex
    return f"{prefix}_{identifier}" if prefix else identifier


class SequenceIdFactory:
    """Deterministic identifier source for tests and fixtures."""

    def __init__(self, prefix: str = "id", start: int = 1) -> None:
        if not prefix or not prefix.replace("_", "").isalnum():
            raise ValueError("sequence prefix must be alphanumeric")
        if start < 0:
            raise ValueError("sequence start cannot be negative")
        self.prefix = prefix
        self.current = start

    def __call__(self) -> str:
        value = f"{self.prefix}_{self.current:08d}"
        self.current += 1
        return value

    def peek(self) -> str:
        return f"{self.prefix}_{self.current:08d}"


IdentifierFactory = Callable[[], str]
