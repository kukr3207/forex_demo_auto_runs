"""Ordered SQLite schema migrations.

Migrations are append-only. Existing statements must never be edited after a
release because each database records the highest successfully applied version.
Every migration runs inside the caller's transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence, Tuple


@dataclass(frozen=True)
class Migration:
    """One atomic database schema transition."""

    version: int
    name: str
    statements: Tuple[str, ...]

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("migration version must be positive")
        if not self.name.strip():
            raise ValueError("migration name cannot be blank")
        if not self.statements:
            raise ValueError("migration requires at least one statement")
        if any(not statement.strip() for statement in self.statements):
            raise ValueError("migration statements cannot be blank")


MIGRATIONS: Tuple[Migration, ...] = (
    Migration(
        1,
        "create_core_market_data",
        (
            """
            CREATE TABLE instruments (
                symbol TEXT PRIMARY KEY COLLATE NOCASE,
                base_currency TEXT NOT NULL,
                quote_currency TEXT NOT NULL,
                precision INTEGER NOT NULL CHECK (precision BETWEEN 0 AND 12),
                pip_size TEXT NOT NULL,
                active INTEGER NOT NULL CHECK (active IN (0, 1)),
                display_name TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE quotes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL COLLATE NOCASE,
                provider TEXT NOT NULL,
                source_id TEXT,
                bid TEXT NOT NULL,
                ask TEXT NOT NULL,
                volume TEXT,
                observed_at TEXT NOT NULL,
                received_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY (symbol) REFERENCES instruments(symbol),
                UNIQUE (provider, symbol, observed_at, source_id)
            )
            """,
            "CREATE INDEX quotes_symbol_time_idx ON quotes(symbol, observed_at DESC)",
            "CREATE INDEX quotes_provider_time_idx ON quotes(provider, observed_at DESC)",
            """
            CREATE TABLE ingestion_runs (
                id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                requested_symbols_json TEXT NOT NULL,
                imported_quotes INTEGER NOT NULL DEFAULT 0,
                duplicate_quotes INTEGER NOT NULL DEFAULT 0,
                rejected_quotes INTEGER NOT NULL DEFAULT 0,
                error_code TEXT,
                error_message TEXT
            )
            """,
            "CREATE INDEX ingestion_runs_started_idx ON ingestion_runs(started_at DESC)",
        ),
    ),
    Migration(
        2,
        "create_candles_and_indicators",
        (
            """
            CREATE TABLE candles (
                symbol TEXT NOT NULL COLLATE NOCASE,
                timeframe TEXT NOT NULL,
                opened_at TEXT NOT NULL,
                closed_at TEXT NOT NULL,
                open TEXT NOT NULL,
                high TEXT NOT NULL,
                low TEXT NOT NULL,
                close TEXT NOT NULL,
                volume TEXT NOT NULL,
                sample_count INTEGER NOT NULL CHECK (sample_count > 0),
                complete INTEGER NOT NULL CHECK (complete IN (0, 1)),
                created_at TEXT NOT NULL,
                PRIMARY KEY (symbol, timeframe, opened_at),
                FOREIGN KEY (symbol) REFERENCES instruments(symbol)
            )
            """,
            "CREATE INDEX candles_symbol_time_idx ON candles(symbol, timeframe, opened_at DESC)",
            """
            CREATE TABLE indicator_values (
                symbol TEXT NOT NULL COLLATE NOCASE,
                timeframe TEXT NOT NULL,
                name TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                value TEXT NOT NULL,
                parameters_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                PRIMARY KEY (symbol, timeframe, name, observed_at),
                FOREIGN KEY (symbol) REFERENCES instruments(symbol)
            )
            """,
            """
            CREATE INDEX indicator_lookup_idx
            ON indicator_values(symbol, timeframe, name, observed_at DESC)
            """,
        ),
    ),
    Migration(
        3,
        "create_alerting",
        (
            """
            CREATE TABLE alert_rules (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                symbol TEXT NOT NULL COLLATE NOCASE,
                metric TEXT NOT NULL,
                operator TEXT NOT NULL,
                threshold TEXT NOT NULL,
                cooldown_seconds INTEGER NOT NULL,
                enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_triggered_at TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY (symbol) REFERENCES instruments(symbol)
            )
            """,
            "CREATE INDEX alert_rules_symbol_idx ON alert_rules(symbol, enabled)",
            """
            CREATE TABLE alerts (
                id TEXT PRIMARY KEY,
                rule_id TEXT NOT NULL,
                symbol TEXT NOT NULL COLLATE NOCASE,
                metric TEXT NOT NULL,
                observed_value TEXT NOT NULL,
                threshold TEXT NOT NULL,
                operator TEXT NOT NULL,
                triggered_at TEXT NOT NULL,
                message TEXT NOT NULL,
                status TEXT NOT NULL,
                delivered_at TEXT,
                acknowledged_at TEXT,
                delivery_error TEXT,
                FOREIGN KEY (rule_id) REFERENCES alert_rules(id),
                FOREIGN KEY (symbol) REFERENCES instruments(symbol)
            )
            """,
            "CREATE INDEX alerts_status_time_idx ON alerts(status, triggered_at)",
            "CREATE INDEX alerts_rule_time_idx ON alerts(rule_id, triggered_at DESC)",
        ),
    ),
    Migration(
        4,
        "create_operational_state",
        (
            """
            CREATE TABLE scheduler_locks (
                name TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                schedule_seconds INTEGER NOT NULL CHECK (schedule_seconds > 0),
                enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                next_run_at TEXT NOT NULL,
                last_started_at TEXT,
                last_finished_at TEXT,
                last_status TEXT,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                payload_json TEXT NOT NULL DEFAULT '{}'
            )
            """,
            "CREATE INDEX jobs_due_idx ON jobs(enabled, next_run_at)",
            """
            CREATE TABLE key_value_state (
                namespace TEXT NOT NULL,
                key TEXT NOT NULL,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (namespace, key)
            )
            """,
        ),
    ),
)


def validate_migrations(migrations: Sequence[Migration] = MIGRATIONS) -> None:
    """Reject duplicate, missing, or unordered migration versions."""

    expected = 1
    names = set()
    for migration in migrations:
        if migration.version != expected:
            raise ValueError(
                f"migration version {migration.version} is out of order; expected {expected}"
            )
        if migration.name in names:
            raise ValueError(f"duplicate migration name: {migration.name}")
        names.add(migration.name)
        expected += 1


def pending_migrations(current_version: int) -> Iterable[Migration]:
    """Yield migrations newer than the current schema version."""

    validate_migrations()
    if current_version < 0:
        raise ValueError("schema version cannot be negative")
    return (migration for migration in MIGRATIONS if migration.version > current_version)


validate_migrations()
