"""Timestamped operational events with stable serialization."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Mapping

from forex_monitor.models import ensure_utc, format_timestamp


class EventLevel(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class OperationalEvent:
    name: str
    level: EventLevel
    occurred_at: datetime
    attributes: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("event name cannot be blank")
        object.__setattr__(self, "name", self.name.strip().lower())
        object.__setattr__(self, "occurred_at", ensure_utc(self.occurred_at))
        object.__setattr__(self, "attributes", dict(self.attributes))

    def as_dict(self) -> Mapping[str, object]:
        return {
            "name": self.name,
            "level": self.level.value,
            "occurredAt": format_timestamp(self.occurred_at),
            "attributes": dict(self.attributes),
        }
