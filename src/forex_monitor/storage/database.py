"""Safe SQLite connection and transaction management."""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Generator, Iterable, Iterator, Mapping, Optional, Sequence

from forex_monitor.config import DatabaseConfig
from forex_monitor.errors import StorageError
from forex_monitor.storage.migrations import MIGRATIONS, pending_migrations


@dataclass
class Transaction:
    """Thin transaction-bound query API."""

    connection: sqlite3.Connection

    def execute(self, sql: str, parameters: Sequence[object] = ()) -> sqlite3.Cursor:
        try:
            return self.connection.execute(sql, tuple(parameters))
        except sqlite3.DatabaseError as error:
            raise StorageError("database statement failed", details={"sql": sql[:120]}) from error

    def executemany(
        self,
        sql: str,
        parameters: Iterable[Sequence[object]],
    ) -> sqlite3.Cursor:
        try:
            return self.connection.executemany(sql, parameters)
        except sqlite3.DatabaseError as error:
            raise StorageError("database batch statement failed", details={"sql": sql[:120]}) from error

    def fetch_one(
        self,
        sql: str,
        parameters: Sequence[object] = (),
    ) -> Optional[Mapping[str, Any]]:
        row = self.execute(sql, parameters).fetchone()
        return dict(row) if row is not None else None

    def fetch_all(
        self,
        sql: str,
        parameters: Sequence[object] = (),
    ) -> list[Mapping[str, Any]]:
        return [dict(row) for row in self.execute(sql, parameters).fetchall()]

    def scalar(
        self,
        sql: str,
        parameters: Sequence[object] = (),
        *,
        default: object = None,
    ) -> object:
        row = self.execute(sql, parameters).fetchone()
        if row is None:
            return default
        return row[0]


class Database:
    """Connection factory with nested-transaction awareness.

    Each outer transaction owns one connection. Nested transaction contexts use
    savepoints and therefore compose safely across service/repository layers.
    Connections are never shared between threads.
    """

    def __init__(self, config: DatabaseConfig) -> None:
        self.config = config
        self._local = threading.local()
        self._migration_lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self.config.path

    def connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(
                str(self.path),
                timeout=self.config.busy_timeout_seconds,
                isolation_level=None,
                check_same_thread=True,
            )
        except sqlite3.Error as error:
            raise StorageError("could not open database", details={"path": str(self.path)}) from error
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {int(self.config.busy_timeout_seconds * 1000)}")
        connection.execute(f"PRAGMA journal_mode = {self.config.journal_mode}")
        connection.execute(f"PRAGMA foreign_keys = {'ON' if self.config.foreign_keys else 'OFF'}")
        return connection

    def initialize(self) -> int:
        """Apply all pending migrations and return the resulting version."""

        if self.path != Path(":memory:"):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._migration_lock:
            connection = self.connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version INTEGER PRIMARY KEY,
                        name TEXT NOT NULL UNIQUE,
                        applied_at TEXT NOT NULL DEFAULT (
                            strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                        )
                    )
                    """
                )
                current = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
                    ).fetchone()[0]
                )
                for migration in pending_migrations(current):
                    for statement in migration.statements:
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
                        (migration.version, migration.name),
                    )
                    current = migration.version
                connection.execute("COMMIT")
                return current
            except sqlite3.DatabaseError as error:
                with contextlib.suppress(sqlite3.Error):
                    connection.execute("ROLLBACK")
                raise StorageError("database migration failed") from error
            finally:
                connection.close()

    def schema_version(self) -> int:
        """Return zero for an uninitialized database or its current version."""

        if self.path != Path(":memory:") and not self.path.exists():
            return 0
        connection = self.connect()
        try:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone()
            if not exists:
                return 0
            return int(
                connection.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
                ).fetchone()[0]
            )
        finally:
            connection.close()

    @property
    def latest_schema_version(self) -> int:
        return MIGRATIONS[-1].version if MIGRATIONS else 0

    @contextlib.contextmanager
    def transaction(self, *, write: bool = False) -> Generator[Transaction, None, None]:
        """Open an atomic context, using savepoints when already nested."""

        current = getattr(self._local, "connection", None)
        depth = int(getattr(self._local, "depth", 0))
        if current is not None:
            savepoint = f"nested_{depth}"
            current.execute(f"SAVEPOINT {savepoint}")
            self._local.depth = depth + 1
            try:
                yield Transaction(current)
                current.execute(f"RELEASE SAVEPOINT {savepoint}")
            except BaseException:
                current.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                current.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise
            finally:
                self._local.depth = depth
            return
        connection = self.connect()
        self._local.connection = connection
        self._local.depth = 1
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield Transaction(connection)
            connection.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                connection.execute("ROLLBACK")
            raise
        finally:
            self._local.connection = None
            self._local.depth = 0
            connection.close()

    def execute_script(self, statements: str) -> None:
        """Execute trusted administrative SQL outside repository paths."""

        connection = self.connect()
        try:
            connection.executescript(statements)
        except sqlite3.DatabaseError as error:
            raise StorageError("database script failed") from error
        finally:
            connection.close()

    def health(self) -> Mapping[str, object]:
        """Return an observable database health summary."""

        try:
            with self.transaction() as transaction:
                quick_check = str(transaction.scalar("PRAGMA quick_check", default="unknown"))
                version = int(
                    transaction.scalar(
                        "SELECT COALESCE(MAX(version), 0) FROM schema_migrations",
                        default=0,
                    )
                )
        except StorageError as error:
            return {"healthy": False, "error": str(error), "path": str(self.path)}
        return {
            "healthy": quick_check == "ok" and version == self.latest_schema_version,
            "quickCheck": quick_check,
            "schemaVersion": version,
            "latestSchemaVersion": self.latest_schema_version,
            "path": str(self.path),
        }

