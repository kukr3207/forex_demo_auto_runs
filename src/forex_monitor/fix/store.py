"""Sequence-aware FIX message persistence and resend lookup."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Optional, Tuple

from forex_monitor.errors import StorageConflictError, ValidationError
from forex_monitor.fix.codec import FixMessage
from forex_monitor.models import ensure_utc


@dataclass(frozen=True)
class StoredFixMessage:
    direction: str
    sequence_number: int
    message: FixMessage
    raw: bytes
    stored_at: datetime

    def __post_init__(self) -> None:
        if self.direction not in {"inbound", "outbound"}:
            raise ValidationError("FIX message direction must be inbound or outbound")
        if self.sequence_number != self.message.sequence_number:
            raise ValidationError("stored sequence number must match the FIX message")
        object.__setattr__(self, "stored_at", ensure_utc(self.stored_at, "FIX store time"))


class InMemoryMessageStore:
    def __init__(self) -> None:
        self._messages: Dict[Tuple[str, int], StoredFixMessage] = {}
        self._next_inbound = 1
        self._next_outbound = 1

    @property
    def next_inbound(self) -> int:
        return self._next_inbound

    @property
    def next_outbound(self) -> int:
        return self._next_outbound

    def save(self, value: StoredFixMessage) -> StoredFixMessage:
        key = (value.direction, value.sequence_number)
        existing = self._messages.get(key)
        if existing is not None:
            if existing == value:
                return existing
            raise StorageConflictError("FIX sequence already contains a different message")
        expected = self._next_inbound if value.direction == "inbound" else self._next_outbound
        if value.sequence_number != expected:
            raise StorageConflictError("FIX sequence is not contiguous")
        self._messages[key] = value
        if value.direction == "inbound":
            self._next_inbound += 1
        else:
            self._next_outbound += 1
        return value

    def get(self, direction: str, sequence_number: int) -> Optional[StoredFixMessage]:
        return self._messages.get((direction, sequence_number))

    def range(self, direction: str, begin: int, end: int = 0) -> Tuple[StoredFixMessage, ...]:
        if direction not in {"inbound", "outbound"}:
            raise ValidationError("FIX message direction must be inbound or outbound")
        if begin <= 0 or end < 0 or (end and end < begin):
            raise ValidationError("FIX resend range is invalid")
        maximum = end or (self._next_inbound - 1 if direction == "inbound" else self._next_outbound - 1)
        return tuple(
            self._messages[(direction, sequence)]
            for sequence in range(begin, maximum + 1)
            if (direction, sequence) in self._messages
        )

    def reset(self, inbound: int = 1, outbound: int = 1) -> None:
        if inbound <= 0 or outbound <= 0:
            raise ValidationError("FIX reset sequences must be positive")
        self._messages.clear()
        self._next_inbound = inbound
        self._next_outbound = outbound
