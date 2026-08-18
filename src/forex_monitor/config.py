"""Validated application configuration loaded from mappings or the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Optional, Tuple

from forex_monitor.errors import ConfigurationError


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{name} must be a non-empty string")
    return value.strip()


def _positive_float(value: object, name: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ConfigurationError(f"{name} must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{name} must be a number") from exc
    if number <= minimum:
        raise ConfigurationError(f"{name} must be greater than {minimum}")
    return number


def _positive_int(value: object, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ConfigurationError(f"{name} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if str(number) != str(value).strip() and not isinstance(value, int):
        raise ConfigurationError(f"{name} must be an integer")
    if number <= minimum:
        raise ConfigurationError(f"{name} must be greater than {minimum}")
    return number


def _nonnegative_float(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ConfigurationError(f"{name} must be a number")
    try:
        number = float(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc
    if number < 0:
        raise ConfigurationError(f"{name} cannot be negative")
    return number


def _csv(value: object, name: str) -> Tuple[str, ...]:
    if isinstance(value, str):
        raw = value.split(",")
    elif isinstance(value, Iterable):
        raw = list(value)
    else:
        raise ConfigurationError(f"{name} must be a comma-separated string")
    normalized = []
    seen = set()
    for item in raw:
        text = _required_text(item, name).upper()
        if text not in seen:
            normalized.append(text)
            seen.add(text)
    if not normalized:
        raise ConfigurationError(f"{name} must contain at least one value")
    return tuple(normalized)


@dataclass(frozen=True)
class DatabaseConfig:
    """SQLite connection and lifecycle settings."""

    path: Path = Path("forex_monitor.db")
    busy_timeout_seconds: float = 5.0
    journal_mode: str = "WAL"
    foreign_keys: bool = True

    def __post_init__(self) -> None:
        if not str(self.path):
            raise ConfigurationError("database path cannot be blank")
        if self.busy_timeout_seconds <= 0:
            raise ConfigurationError("database busy timeout must be positive")
        journal_mode = self.journal_mode.upper()
        if journal_mode not in {"DELETE", "TRUNCATE", "PERSIST", "MEMORY", "WAL", "OFF"}:
            raise ConfigurationError("database journal mode is not supported")
        object.__setattr__(self, "journal_mode", journal_mode)

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> DatabaseConfig:
        return cls(
            path=Path(str(values.get("path", "forex_monitor.db"))),
            busy_timeout_seconds=_positive_float(
                values.get("busy_timeout_seconds", 5.0),
                "database busy timeout",
            ),
            journal_mode=str(values.get("journal_mode", "WAL")),
            foreign_keys=bool(values.get("foreign_keys", True)),
        )


@dataclass(frozen=True)
class ProviderConfig:
    """Market-data endpoint and retry policy."""

    name: str = "fxssi"
    base_url: str = "https://c.fxssi.com/api/current-ratios"
    api_key: Optional[str] = None
    timeout_seconds: float = 10.0
    max_attempts: int = 3
    initial_backoff_seconds: float = 0.25
    user_agent: str = "forex-monitor/1.0"

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _required_text(self.name, "provider name").lower())
        base_url = _required_text(self.base_url, "provider base URL")
        if not base_url.startswith(("https://", "http://")):
            raise ConfigurationError("provider base URL must use http or https")
        object.__setattr__(self, "base_url", base_url.rstrip("/"))
        if self.timeout_seconds <= 0:
            raise ConfigurationError("provider timeout must be positive")
        if self.max_attempts < 1 or self.max_attempts > 10:
            raise ConfigurationError("provider max attempts must be between 1 and 10")
        if self.initial_backoff_seconds < 0:
            raise ConfigurationError("provider backoff cannot be negative")
        if self.api_key is not None:
            key = self.api_key.strip()
            object.__setattr__(self, "api_key", key or None)

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> ProviderConfig:
        return cls(
            name=str(values.get("name", "fxssi")),
            base_url=str(values.get("base_url", "https://c.fxssi.com/api/current-ratios")),
            api_key=str(values["api_key"]) if values.get("api_key") else None,
            timeout_seconds=_positive_float(
                values.get("timeout_seconds", 10.0),
                "provider timeout",
            ),
            max_attempts=_positive_int(values.get("max_attempts", 3), "provider max attempts"),
            initial_backoff_seconds=_nonnegative_float(
                values.get("initial_backoff_seconds", 0.25),
                "provider initial backoff",
            ),
            user_agent=str(values.get("user_agent", "forex-monitor/1.0")),
        )


@dataclass(frozen=True)
class RuntimeConfig:
    """Ingestion cadence, instruments, retention, and operational limits."""

    instruments: Tuple[str, ...] = (
        "EURUSD",
        "GBPUSD",
        "USDJPY",
        "AUDUSD",
        "USDCAD",
    )
    interval_seconds: int = 3600
    retention_days: int = 365
    batch_size: int = 500
    fail_fast: bool = False
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        object.__setattr__(self, "instruments", _csv(self.instruments, "instruments"))
        if self.interval_seconds < 1:
            raise ConfigurationError("runtime interval must be positive")
        if self.retention_days < 1:
            raise ConfigurationError("retention days must be positive")
        if self.batch_size < 1 or self.batch_size > 100_000:
            raise ConfigurationError("batch size must be between 1 and 100000")
        level = self.log_level.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigurationError("log level is not supported")
        object.__setattr__(self, "log_level", level)

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> RuntimeConfig:
        return cls(
            instruments=_csv(
                values.get("instruments", "EURUSD,GBPUSD,USDJPY,AUDUSD,USDCAD"),
                "instruments",
            ),
            interval_seconds=_positive_int(
                values.get("interval_seconds", 3600),
                "runtime interval",
            ),
            retention_days=_positive_int(values.get("retention_days", 365), "retention days"),
            batch_size=_positive_int(values.get("batch_size", 500), "batch size"),
            fail_fast=bool(values.get("fail_fast", False)),
            log_level=str(values.get("log_level", "INFO")),
        )


@dataclass(frozen=True)
class AppConfig:
    """Root configuration object shared by CLI commands and services."""

    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    provider: ProviderConfig = field(default_factory=ProviderConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> AppConfig:
        database_values = values.get("database", {})
        provider_values = values.get("provider", {})
        runtime_values = values.get("runtime", {})
        if not isinstance(database_values, Mapping):
            raise ConfigurationError("database configuration must be an object")
        if not isinstance(provider_values, Mapping):
            raise ConfigurationError("provider configuration must be an object")
        if not isinstance(runtime_values, Mapping):
            raise ConfigurationError("runtime configuration must be an object")
        return cls(
            database=DatabaseConfig.from_mapping(database_values),
            provider=ProviderConfig.from_mapping(provider_values),
            runtime=RuntimeConfig.from_mapping(runtime_values),
        )

    @classmethod
    def from_environment(cls, environ: Optional[Mapping[str, str]] = None) -> AppConfig:
        env = dict(os.environ if environ is None else environ)
        return cls(
            database=DatabaseConfig(
                path=Path(env.get("FOREX_DATABASE_PATH", "forex_monitor.db")),
                busy_timeout_seconds=_positive_float(
                    env.get("FOREX_DATABASE_BUSY_TIMEOUT", "5"),
                    "FOREX_DATABASE_BUSY_TIMEOUT",
                ),
                journal_mode=env.get("FOREX_DATABASE_JOURNAL_MODE", "WAL"),
                foreign_keys=env.get("FOREX_DATABASE_FOREIGN_KEYS", "true").lower()
                not in {"0", "false", "no", "off"},
            ),
            provider=ProviderConfig(
                name=env.get("FOREX_PROVIDER", "fxssi"),
                base_url=env.get(
                    "FOREX_PROVIDER_URL",
                    "https://c.fxssi.com/api/current-ratios",
                ),
                api_key=env.get("FOREX_PROVIDER_API_KEY"),
                timeout_seconds=_positive_float(
                    env.get("FOREX_PROVIDER_TIMEOUT", "10"),
                    "FOREX_PROVIDER_TIMEOUT",
                ),
                max_attempts=_positive_int(
                    env.get("FOREX_PROVIDER_MAX_ATTEMPTS", "3"),
                    "FOREX_PROVIDER_MAX_ATTEMPTS",
                ),
                initial_backoff_seconds=float(env.get("FOREX_PROVIDER_BACKOFF", "0.25")),
                user_agent=env.get("FOREX_PROVIDER_USER_AGENT", "forex-monitor/1.0"),
            ),
            runtime=RuntimeConfig(
                instruments=_csv(
                    env.get("FOREX_INSTRUMENTS", "EURUSD,GBPUSD,USDJPY,AUDUSD,USDCAD"),
                    "FOREX_INSTRUMENTS",
                ),
                interval_seconds=_positive_int(
                    env.get("FOREX_INTERVAL_SECONDS", "3600"),
                    "FOREX_INTERVAL_SECONDS",
                ),
                retention_days=_positive_int(
                    env.get("FOREX_RETENTION_DAYS", "365"),
                    "FOREX_RETENTION_DAYS",
                ),
                batch_size=_positive_int(
                    env.get("FOREX_BATCH_SIZE", "500"),
                    "FOREX_BATCH_SIZE",
                ),
                fail_fast=env.get("FOREX_FAIL_FAST", "false").lower() in {"1", "true", "yes", "on"},
                log_level=env.get("FOREX_LOG_LEVEL", "INFO"),
            ),
        )
