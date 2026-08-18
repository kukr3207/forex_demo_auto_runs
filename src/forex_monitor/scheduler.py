"""Cooperative persistent scheduler with expiring SQLite leases."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable, Dict, Mapping, Optional, Protocol, Sequence, Tuple

from forex_monitor.errors import NotFoundError, StorageConflictError
from forex_monitor.ids import IdentifierFactory, uuid_hex
from forex_monitor.models import ensure_utc, format_timestamp, parse_timestamp
from forex_monitor.storage import Database, Transaction


class JobStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class ScheduledJob:
    """Persistent recurring job definition and recent runtime state."""

    id: str
    name: str
    schedule_seconds: int
    enabled: bool
    next_run_at: datetime
    last_started_at: Optional[datetime] = None
    last_finished_at: Optional[datetime] = None
    last_status: Optional[JobStatus] = None
    consecutive_failures: int = 0
    payload: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        identifier = self.id.strip()
        name = self.name.strip()
        if not identifier or len(identifier) > 80:
            raise ValueError("job id must contain 1-80 characters")
        if not name or len(name) > 100:
            raise ValueError("job name must contain 1-100 characters")
        if self.schedule_seconds < 1 or self.schedule_seconds > 31_536_000:
            raise ValueError("job schedule must be between 1 second and 1 year")
        if self.consecutive_failures < 0:
            raise ValueError("consecutive failures cannot be negative")
        next_run_at = ensure_utc(self.next_run_at, "next run at")
        last_started = ensure_utc(self.last_started_at) if self.last_started_at else None
        last_finished = ensure_utc(self.last_finished_at) if self.last_finished_at else None
        if last_finished and last_started and last_finished < last_started:
            raise ValueError("job finish cannot precede its start")
        normalized_payload = {}
        for key, value in self.payload.items():
            if not isinstance(key, str) or not key:
                raise ValueError("job payload keys must be non-empty strings")
            if not isinstance(value, (str, int, float, bool, type(None), list, dict)):
                raise ValueError("job payload must be JSON compatible")
            normalized_payload[key] = value
        object.__setattr__(self, "id", identifier)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "next_run_at", next_run_at)
        object.__setattr__(self, "last_started_at", last_started)
        object.__setattr__(self, "last_finished_at", last_finished)
        object.__setattr__(self, "payload", normalized_payload)

    def next_after(self, reference: datetime) -> datetime:
        """Return the first scheduled instant strictly after reference."""

        current = self.next_run_at
        reference = ensure_utc(reference)
        step = timedelta(seconds=self.schedule_seconds)
        while current <= reference:
            current += step
        return current


@dataclass(frozen=True)
class JobExecution:
    job_id: str
    owner_id: str
    started_at: datetime
    finished_at: datetime
    status: JobStatus
    message: Optional[str] = None

    @property
    def duration_seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()


class JobHandler(Protocol):
    def __call__(self, job: ScheduledJob) -> Optional[str]:
        ...


@dataclass
class JobRepository:
    database: Database

    def save(self, job: ScheduledJob, *, transaction: Optional[Transaction] = None) -> ScheduledJob:
        if transaction is None:
            with self.database.transaction(write=True) as active:
                return self.save(job, transaction=active)
        transaction.execute(
            """
            INSERT INTO jobs(
                id, name, schedule_seconds, enabled, next_run_at, last_started_at,
                last_finished_at, last_status, consecutive_failures, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                schedule_seconds=excluded.schedule_seconds,
                enabled=excluded.enabled,
                next_run_at=excluded.next_run_at,
                last_started_at=excluded.last_started_at,
                last_finished_at=excluded.last_finished_at,
                last_status=excluded.last_status,
                consecutive_failures=excluded.consecutive_failures,
                payload_json=excluded.payload_json
            """,
            (
                job.id,
                job.name,
                job.schedule_seconds,
                int(job.enabled),
                format_timestamp(job.next_run_at),
                format_timestamp(job.last_started_at) if job.last_started_at else None,
                format_timestamp(job.last_finished_at) if job.last_finished_at else None,
                job.last_status.value if job.last_status else None,
                job.consecutive_failures,
                json.dumps(job.payload, sort_keys=True, separators=(",", ":")),
            ),
        )
        return job

    def get(self, job_id: str) -> Optional[ScheduledJob]:
        with self.database.transaction() as transaction:
            row = transaction.fetch_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
            return self._from_row(row) if row else None

    def due(self, at: datetime, *, limit: int = 100) -> Tuple[ScheduledJob, ...]:
        if limit < 1 or limit > 1000:
            raise ValueError("due-job limit must be between 1 and 1000")
        with self.database.transaction() as transaction:
            rows = transaction.fetch_all(
                """
                SELECT * FROM jobs
                WHERE enabled = 1 AND next_run_at <= ?
                ORDER BY next_run_at, id LIMIT ?
                """,
                (format_timestamp(at), limit),
            )
            return tuple(self._from_row(row) for row in rows)

    def list(self) -> Tuple[ScheduledJob, ...]:
        with self.database.transaction() as transaction:
            return tuple(
                self._from_row(row)
                for row in transaction.fetch_all("SELECT * FROM jobs ORDER BY name, id")
            )

    def delete(self, job_id: str) -> bool:
        with self.database.transaction(write=True) as transaction:
            return transaction.execute("DELETE FROM jobs WHERE id = ?", (job_id,)).rowcount == 1

    @staticmethod
    def _from_row(row: Mapping[str, object]) -> ScheduledJob:
        try:
            payload = json.loads(str(row["payload_json"]))
        except json.JSONDecodeError as error:
            raise ValueError("stored job payload is malformed") from error
        return ScheduledJob(
            id=str(row["id"]),
            name=str(row["name"]),
            schedule_seconds=int(row["schedule_seconds"]),
            enabled=bool(row["enabled"]),
            next_run_at=parse_timestamp(row["next_run_at"]),
            last_started_at=(
                parse_timestamp(row["last_started_at"]) if row["last_started_at"] else None
            ),
            last_finished_at=(
                parse_timestamp(row["last_finished_at"]) if row["last_finished_at"] else None
            ),
            last_status=JobStatus(str(row["last_status"])) if row["last_status"] else None,
            consecutive_failures=int(row["consecutive_failures"]),
            payload=payload,
        )


@dataclass
class LeaseRepository:
    database: Database

    def acquire(
        self,
        name: str,
        owner_id: str,
        *,
        now: datetime,
        ttl_seconds: int,
    ) -> bool:
        if ttl_seconds < 1:
            raise ValueError("lease duration must be positive")
        now = ensure_utc(now)
        expires_at = now + timedelta(seconds=ttl_seconds)
        with self.database.transaction(write=True) as transaction:
            transaction.execute(
                "DELETE FROM scheduler_locks WHERE name = ? AND expires_at <= ?",
                (name, format_timestamp(now)),
            )
            cursor = transaction.execute(
                """
                INSERT OR IGNORE INTO scheduler_locks(name, owner_id, acquired_at, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (name, owner_id, format_timestamp(now), format_timestamp(expires_at)),
            )
            return cursor.rowcount == 1

    def extend(
        self,
        name: str,
        owner_id: str,
        *,
        now: datetime,
        ttl_seconds: int,
    ) -> bool:
        now = ensure_utc(now)
        expires_at = now + timedelta(seconds=ttl_seconds)
        with self.database.transaction(write=True) as transaction:
            cursor = transaction.execute(
                """
                UPDATE scheduler_locks SET expires_at = ?
                WHERE name = ? AND owner_id = ? AND expires_at > ?
                """,
                (format_timestamp(expires_at), name, owner_id, format_timestamp(now)),
            )
            return cursor.rowcount == 1

    def release(self, name: str, owner_id: str) -> bool:
        with self.database.transaction(write=True) as transaction:
            cursor = transaction.execute(
                "DELETE FROM scheduler_locks WHERE name = ? AND owner_id = ?",
                (name, owner_id),
            )
            return cursor.rowcount == 1


@dataclass
class Scheduler:
    database: Database
    jobs: JobRepository
    leases: LeaseRepository
    handlers: Mapping[str, JobHandler]
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))
    identifiers: IdentifierFactory = uuid_hex
    lease_seconds: int = 300

    def __post_init__(self) -> None:
        self.owner_id = self.identifiers()
        self.handlers = dict(self.handlers)

    def run_due(self, *, limit: int = 100) -> Tuple[JobExecution, ...]:
        now = ensure_utc(self.clock())
        executions = []
        for job in self.jobs.due(now, limit=limit):
            execution = self.run_job(job.id)
            if execution is not None:
                executions.append(execution)
        return tuple(executions)

    def run_job(self, job_id: str) -> Optional[JobExecution]:
        job = self.jobs.get(job_id)
        if job is None:
            raise NotFoundError("scheduled job was not found", details={"id": job_id})
        if not job.enabled:
            return None
        handler = self.handlers.get(job.name)
        if handler is None:
            raise NotFoundError("scheduled job handler was not registered", details={"name": job.name})
        lock_name = f"job:{job.id}"
        started_at = ensure_utc(self.clock())
        if not self.leases.acquire(
            lock_name,
            self.owner_id,
            now=started_at,
            ttl_seconds=self.lease_seconds,
        ):
            return None
        started = replace(job, last_started_at=started_at)
        self.jobs.save(started)
        message = None
        status = JobStatus.SUCCEEDED
        try:
            message = handler(started)
        except Exception as error:
            status = JobStatus.FAILED
            message = str(error)[:1000]
        finished_at = ensure_utc(self.clock())
        completed = replace(
            started,
            next_run_at=started.next_after(finished_at),
            last_finished_at=finished_at,
            last_status=status,
            consecutive_failures=(started.consecutive_failures + 1 if status is JobStatus.FAILED else 0),
        )
        try:
            self.jobs.save(completed)
        finally:
            self.leases.release(lock_name, self.owner_id)
        return JobExecution(
            job_id=job.id,
            owner_id=self.owner_id,
            started_at=started_at,
            finished_at=finished_at,
            status=status,
            message=message,
        )


def create_scheduler(
    database: Database,
    handlers: Mapping[str, JobHandler],
    **kwargs: object,
) -> Scheduler:
    """Construct a scheduler whose repositories share one database."""

    return Scheduler(
        database=database,
        jobs=JobRepository(database),
        leases=LeaseRepository(database),
        handlers=handlers,
        **kwargs,  # type: ignore[arg-type]
    )
