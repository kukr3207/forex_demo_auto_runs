"""Notification delivery interfaces and local implementations."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Protocol, Sequence, Tuple

from forex_monitor.models import Alert, format_timestamp


@dataclass(frozen=True)
class NotificationResult:
    """Outcome from one notifier without leaking transport-specific objects."""

    notifier: str
    delivered: bool
    attempted_at: datetime
    external_id: Optional[str] = None
    error: Optional[str] = None

    def __post_init__(self) -> None:
        name = self.notifier.strip().lower()
        if not name:
            raise ValueError("notifier name cannot be blank")
        if self.delivered and self.error:
            raise ValueError("a delivered notification cannot contain an error")
        if not self.delivered and not self.error:
            raise ValueError("a failed notification requires an error")
        object.__setattr__(self, "notifier", name)


class Notifier(Protocol):
    """Alert delivery contract."""

    @property
    def name(self) -> str:
        ...

    def notify(self, alert: Alert) -> NotificationResult:
        ...


@dataclass
class MemoryNotifier:
    """Collect alerts in memory for tests and embedded applications."""

    notifier_name: str = "memory"
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))
    fail_with: Optional[str] = None

    def __post_init__(self) -> None:
        self.alerts: List[Alert] = []

    @property
    def name(self) -> str:
        return self.notifier_name

    def notify(self, alert: Alert) -> NotificationResult:
        attempted_at = self.clock()
        if self.fail_with:
            return NotificationResult(self.name, False, attempted_at, error=self.fail_with)
        self.alerts.append(alert)
        return NotificationResult(
            self.name,
            True,
            attempted_at,
            external_id=f"memory:{len(self.alerts)}",
        )


@dataclass
class ConsoleNotifier:
    """Write newline-delimited alert JSON to a text stream."""

    stream: object = sys.stdout
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))

    @property
    def name(self) -> str:
        return "console"

    def notify(self, alert: Alert) -> NotificationResult:
        attempted_at = self.clock()
        try:
            self.stream.write(json.dumps(alert.as_dict(), sort_keys=True) + "\n")
            self.stream.flush()
        except (AttributeError, OSError) as error:
            return NotificationResult(self.name, False, attempted_at, error=str(error))
        return NotificationResult(self.name, True, attempted_at, external_id=alert.id)


@dataclass
class JsonlFileNotifier:
    """Append durable, deterministic JSON records to a local file."""

    path: Path
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))

    @property
    def name(self) -> str:
        return "jsonl_file"

    def notify(self, alert: Alert) -> NotificationResult:
        attempted_at = self.clock()
        record = {
            "notificationId": f"{alert.id}:{format_timestamp(attempted_at)}",
            "attemptedAt": format_timestamp(attempted_at),
            "alert": alert.as_dict(),
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
        except OSError as error:
            return NotificationResult(self.name, False, attempted_at, error=str(error))
        return NotificationResult(
            self.name,
            True,
            attempted_at,
            external_id=str(record["notificationId"]),
        )


@dataclass
class CompositeNotifier:
    """Deliver through every child and report success only when all succeed."""

    notifiers: Sequence[Notifier]
    require_all: bool = True

    def __post_init__(self) -> None:
        self.notifiers = tuple(self.notifiers)
        if not self.notifiers:
            raise ValueError("composite notifier requires at least one child")
        names = [notifier.name for notifier in self.notifiers]
        if len(set(names)) != len(names):
            raise ValueError("composite notifier child names must be unique")

    @property
    def name(self) -> str:
        return "composite"

    def notify_all(self, alert: Alert) -> Tuple[NotificationResult, ...]:
        return tuple(notifier.notify(alert) for notifier in self.notifiers)

    def notify(self, alert: Alert) -> NotificationResult:
        results = self.notify_all(alert)
        delivered = all(result.delivered for result in results) if self.require_all else any(
            result.delivered for result in results
        )
        errors = "; ".join(
            f"{result.notifier}: {result.error}" for result in results if result.error
        )
        attempted_at = max(result.attempted_at for result in results)
        return NotificationResult(
            self.name,
            delivered,
            attempted_at,
            external_id=(
                ",".join(result.external_id or result.notifier for result in results if result.delivered)
                if delivered
                else None
            ),
            error=None if delivered else errors or "no notification channel accepted the alert",
        )

