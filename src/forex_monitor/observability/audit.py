"""Bounded in-memory audit event collection."""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from forex_monitor.observability.events import EventLevel, OperationalEvent


@dataclass
class AuditLog:
    capacity: int = 1000
    _events: List[OperationalEvent] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.capacity < 1:
            raise ValueError("audit capacity must be positive")

    def append(self, event: OperationalEvent) -> None:
        self._events.append(event)
        if len(self._events) > self.capacity:
            del self._events[: len(self._events) - self.capacity]

    def list(
        self, level: Optional[EventLevel] = None, limit: int = 100
    ) -> Tuple[OperationalEvent, ...]:
        if limit < 1:
            raise ValueError("audit limit must be positive")
        events = (
            self._events
            if level is None
            else [event for event in self._events if event.level is level]
        )
        return tuple(events[-limit:])
