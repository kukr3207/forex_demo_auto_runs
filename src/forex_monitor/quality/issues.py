"""Structured data-quality findings."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping


class IssueSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class QualityIssue:
    code: str
    message: str
    severity: IssueSeverity
    record_id: str
    details: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.code.strip() or not self.message.strip() or not self.record_id.strip():
            raise ValueError("quality issue code, message, and record ID are required")
