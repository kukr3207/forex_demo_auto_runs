"""Domain-oriented repositories backed by the SQLite transaction API."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Iterable, Mapping, Optional, Sequence, Tuple

from forex_monitor.errors import NotFoundError, StorageConflictError, StorageError
from forex_monitor.models import (
    Alert,
    AlertOperator,
    AlertRule,
    AlertStatus,
    Candle,
    IngestionRun,
    Instrument,
    Quote,
    RunStatus,
    Timeframe,
    format_timestamp,
    parse_timestamp,
)
from forex_monitor.storage.database import Database, Transaction


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _json_object(value: object) -> Mapping[str, object]:
    try:
        decoded = json.loads(str(value))
    except (TypeError, json.JSONDecodeError) as error:
        raise StorageError("stored JSON is malformed") from error
    if not isinstance(decoded, dict):
        raise StorageError("stored JSON must be an object")
    return decoded


def _json_strings(value: object) -> Tuple[str, ...]:
    try:
        decoded = json.loads(str(value))
    except (TypeError, json.JSONDecodeError) as error:
        raise StorageError("stored JSON is malformed") from error
    if not isinstance(decoded, list) or not all(isinstance(item, str) for item in decoded):
        raise StorageError("stored JSON must be a string array")
    return tuple(decoded)


def _optional_time(value: object) -> Optional[datetime]:
    return parse_timestamp(value) if value is not None else None


@dataclass
class InstrumentRepository:
    database: Database

    def upsert(
        self,
        instrument: Instrument,
        *,
        now: datetime,
        transaction: Optional[Transaction] = None,
    ) -> Instrument:
        if transaction is None:
            with self.database.transaction(write=True) as active:
                return self.upsert(instrument, now=now, transaction=active)
        timestamp = format_timestamp(now)
        transaction.execute(
            """
            INSERT INTO instruments(
                symbol, base_currency, quote_currency, precision, pip_size,
                active, display_name, metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                base_currency=excluded.base_currency,
                quote_currency=excluded.quote_currency,
                precision=excluded.precision,
                pip_size=excluded.pip_size,
                active=excluded.active,
                display_name=excluded.display_name,
                metadata_json=excluded.metadata_json,
                updated_at=excluded.updated_at
            """,
            (
                instrument.symbol,
                instrument.base_currency,
                instrument.quote_currency,
                instrument.precision,
                str(instrument.pip_size),
                int(instrument.active),
                instrument.display_name,
                _json(instrument.metadata),
                timestamp,
                timestamp,
            ),
        )
        return instrument

    def get(
        self,
        symbol: str,
        *,
        transaction: Optional[Transaction] = None,
    ) -> Optional[Instrument]:
        if transaction is None:
            with self.database.transaction() as active:
                return self.get(symbol, transaction=active)
        row = transaction.fetch_one("SELECT * FROM instruments WHERE symbol = ?", (symbol,))
        return self._from_row(row) if row else None

    def require(self, symbol: str) -> Instrument:
        instrument = self.get(symbol)
        if instrument is None:
            raise NotFoundError("instrument was not found", details={"symbol": symbol})
        return instrument

    def list(self, *, active_only: bool = False) -> Tuple[Instrument, ...]:
        with self.database.transaction() as transaction:
            sql = "SELECT * FROM instruments"
            parameters: Sequence[object] = ()
            if active_only:
                sql += " WHERE active = 1"
            sql += " ORDER BY symbol COLLATE NOCASE"
            return tuple(self._from_row(row) for row in transaction.fetch_all(sql, parameters))

    def set_active(self, symbol: str, active: bool, *, now: datetime) -> bool:
        with self.database.transaction(write=True) as transaction:
            cursor = transaction.execute(
                "UPDATE instruments SET active = ?, updated_at = ? WHERE symbol = ?",
                (int(active), format_timestamp(now), symbol),
            )
            return cursor.rowcount == 1

    @staticmethod
    def _from_row(row: Mapping[str, object]) -> Instrument:
        return Instrument(
            symbol=str(row["symbol"]),
            base_currency=str(row["base_currency"]),
            quote_currency=str(row["quote_currency"]),
            precision=int(row["precision"]),
            pip_size=Decimal(str(row["pip_size"])),
            active=bool(row["active"]),
            display_name=str(row["display_name"]),
            metadata=_json_object(row["metadata_json"]),
        )


@dataclass
class QuoteRepository:
    database: Database

    def add(
        self,
        quote: Quote,
        *,
        received_at: datetime,
        transaction: Optional[Transaction] = None,
    ) -> bool:
        if transaction is None:
            with self.database.transaction(write=True) as active:
                return self.add(quote, received_at=received_at, transaction=active)
        try:
            cursor = transaction.execute(
                """
                INSERT OR IGNORE INTO quotes(
                    symbol, provider, source_id, bid, ask, volume,
                    observed_at, received_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    quote.symbol,
                    quote.provider,
                    quote.source_id,
                    str(quote.bid),
                    str(quote.ask),
                    str(quote.volume) if quote.volume is not None else None,
                    format_timestamp(quote.observed_at),
                    format_timestamp(received_at),
                    _json(quote.metadata),
                ),
            )
        except StorageError as error:
            if isinstance(error.__cause__, sqlite3.IntegrityError):
                raise StorageConflictError(
                    "quote references an unknown instrument",
                    details={"symbol": quote.symbol},
                ) from error
            raise
        return cursor.rowcount == 1

    def add_many(
        self,
        quotes: Iterable[Quote],
        *,
        received_at: datetime,
        transaction: Optional[Transaction] = None,
    ) -> Tuple[int, int]:
        if transaction is None:
            with self.database.transaction(write=True) as active:
                return self.add_many(quotes, received_at=received_at, transaction=active)
        imported = 0
        duplicates = 0
        for quote in quotes:
            if self.add(quote, received_at=received_at, transaction=transaction):
                imported += 1
            else:
                duplicates += 1
        return imported, duplicates

    def latest(
        self,
        symbol: str,
        *,
        provider: Optional[str] = None,
    ) -> Optional[Quote]:
        with self.database.transaction() as transaction:
            sql = "SELECT * FROM quotes WHERE symbol = ?"
            parameters: list[object] = [symbol]
            if provider is not None:
                sql += " AND provider = ?"
                parameters.append(provider.lower())
            sql += " ORDER BY observed_at DESC, id DESC LIMIT 1"
            row = transaction.fetch_one(sql, parameters)
            return self._from_row(row) if row else None

    def range(
        self,
        symbol: str,
        *,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        provider: Optional[str] = None,
        limit: int = 10_000,
        ascending: bool = True,
    ) -> Tuple[Quote, ...]:
        if limit < 1 or limit > 100_000:
            raise ValueError("quote range limit must be between 1 and 100000")
        clauses = ["symbol = ?"]
        parameters: list[object] = [symbol]
        if start is not None:
            clauses.append("observed_at >= ?")
            parameters.append(format_timestamp(start))
        if end is not None:
            clauses.append("observed_at < ?")
            parameters.append(format_timestamp(end))
        if provider is not None:
            clauses.append("provider = ?")
            parameters.append(provider.lower())
        direction = "ASC" if ascending else "DESC"
        parameters.append(limit)
        with self.database.transaction() as transaction:
            rows = transaction.fetch_all(
                f"""
                SELECT * FROM quotes
                WHERE {' AND '.join(clauses)}
                ORDER BY observed_at {direction}, id {direction}
                LIMIT ?
                """,
                parameters,
            )
            return tuple(self._from_row(row) for row in rows)

    def count(self, symbol: Optional[str] = None) -> int:
        with self.database.transaction() as transaction:
            if symbol is None:
                return int(transaction.scalar("SELECT COUNT(*) FROM quotes", default=0))
            return int(
                transaction.scalar(
                    "SELECT COUNT(*) FROM quotes WHERE symbol = ?",
                    (symbol,),
                    default=0,
                )
            )

    def delete_before(self, cutoff: datetime, *, batch_size: int = 10_000) -> int:
        if batch_size < 1:
            raise ValueError("batch size must be positive")
        with self.database.transaction(write=True) as transaction:
            cursor = transaction.execute(
                """
                DELETE FROM quotes WHERE id IN (
                    SELECT id FROM quotes WHERE observed_at < ?
                    ORDER BY observed_at LIMIT ?
                )
                """,
                (format_timestamp(cutoff), batch_size),
            )
            return max(cursor.rowcount, 0)

    @staticmethod
    def _from_row(row: Mapping[str, object]) -> Quote:
        return Quote(
            symbol=str(row["symbol"]),
            provider=str(row["provider"]),
            source_id=str(row["source_id"]) if row["source_id"] is not None else None,
            bid=Decimal(str(row["bid"])),
            ask=Decimal(str(row["ask"])),
            volume=Decimal(str(row["volume"])) if row["volume"] is not None else None,
            observed_at=parse_timestamp(row["observed_at"]),
            metadata=_json_object(row["metadata_json"]),
        )


@dataclass
class CandleRepository:
    database: Database

    def upsert(
        self,
        candle: Candle,
        *,
        now: datetime,
        transaction: Optional[Transaction] = None,
    ) -> Candle:
        if transaction is None:
            with self.database.transaction(write=True) as active:
                return self.upsert(candle, now=now, transaction=active)
        transaction.execute(
            """
            INSERT INTO candles(
                symbol, timeframe, opened_at, closed_at, open, high, low, close,
                volume, sample_count, complete, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, timeframe, opened_at) DO UPDATE SET
                closed_at=excluded.closed_at,
                open=excluded.open,
                high=excluded.high,
                low=excluded.low,
                close=excluded.close,
                volume=excluded.volume,
                sample_count=excluded.sample_count,
                complete=excluded.complete
            """,
            (
                candle.symbol,
                candle.timeframe.value,
                format_timestamp(candle.opened_at),
                format_timestamp(candle.closed_at),
                str(candle.open),
                str(candle.high),
                str(candle.low),
                str(candle.close),
                str(candle.volume),
                candle.sample_count,
                int(candle.complete),
                format_timestamp(now),
            ),
        )
        return candle

    def range(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        limit: int = 5000,
    ) -> Tuple[Candle, ...]:
        clauses = ["symbol = ?", "timeframe = ?"]
        parameters: list[object] = [symbol, timeframe.value]
        if start:
            clauses.append("opened_at >= ?")
            parameters.append(format_timestamp(start))
        if end:
            clauses.append("opened_at < ?")
            parameters.append(format_timestamp(end))
        parameters.append(limit)
        with self.database.transaction() as transaction:
            rows = transaction.fetch_all(
                f"""
                SELECT * FROM candles WHERE {' AND '.join(clauses)}
                ORDER BY opened_at ASC LIMIT ?
                """,
                parameters,
            )
            return tuple(self._from_row(row) for row in rows)

    @staticmethod
    def _from_row(row: Mapping[str, object]) -> Candle:
        return Candle(
            symbol=str(row["symbol"]),
            timeframe=Timeframe.parse(row["timeframe"]),
            opened_at=parse_timestamp(row["opened_at"]),
            closed_at=parse_timestamp(row["closed_at"]),
            open=Decimal(str(row["open"])),
            high=Decimal(str(row["high"])),
            low=Decimal(str(row["low"])),
            close=Decimal(str(row["close"])),
            volume=Decimal(str(row["volume"])),
            sample_count=int(row["sample_count"]),
            complete=bool(row["complete"]),
        )


@dataclass
class IngestionRunRepository:
    database: Database

    def create(
        self,
        run: IngestionRun,
        *,
        transaction: Optional[Transaction] = None,
    ) -> IngestionRun:
        if transaction is None:
            with self.database.transaction(write=True) as active:
                return self.create(run, transaction=active)
        try:
            transaction.execute(
                """
                INSERT INTO ingestion_runs(
                    id, provider, status, started_at, finished_at,
                    requested_symbols_json, imported_quotes, duplicate_quotes,
                    rejected_quotes, error_code, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._values(run),
            )
        except StorageError as error:
            if isinstance(error.__cause__, sqlite3.IntegrityError):
                raise StorageConflictError("ingestion run already exists") from error
            raise
        return run

    def update(
        self,
        run: IngestionRun,
        *,
        transaction: Optional[Transaction] = None,
    ) -> IngestionRun:
        if transaction is None:
            with self.database.transaction(write=True) as active:
                return self.update(run, transaction=active)
        cursor = transaction.execute(
            """
            UPDATE ingestion_runs SET
                status=?, finished_at=?, requested_symbols_json=?, imported_quotes=?,
                duplicate_quotes=?, rejected_quotes=?, error_code=?, error_message=?
            WHERE id=?
            """,
            (
                run.status.value,
                format_timestamp(run.finished_at) if run.finished_at else None,
                _json(run.requested_symbols),
                run.imported_quotes,
                run.duplicate_quotes,
                run.rejected_quotes,
                run.error_code,
                run.error_message,
                run.id,
            ),
        )
        if cursor.rowcount != 1:
            raise NotFoundError("ingestion run was not found", details={"id": run.id})
        return run

    def get(self, run_id: str) -> Optional[IngestionRun]:
        with self.database.transaction() as transaction:
            row = transaction.fetch_one("SELECT * FROM ingestion_runs WHERE id = ?", (run_id,))
            return self._from_row(row) if row else None

    def recent(self, limit: int = 20) -> Tuple[IngestionRun, ...]:
        if limit < 1 or limit > 1000:
            raise ValueError("run limit must be between 1 and 1000")
        with self.database.transaction() as transaction:
            rows = transaction.fetch_all(
                "SELECT * FROM ingestion_runs ORDER BY started_at DESC LIMIT ?",
                (limit,),
            )
            return tuple(self._from_row(row) for row in rows)

    @staticmethod
    def _values(run: IngestionRun) -> Sequence[object]:
        return (
            run.id,
            run.provider,
            run.status.value,
            format_timestamp(run.started_at),
            format_timestamp(run.finished_at) if run.finished_at else None,
            _json(run.requested_symbols),
            run.imported_quotes,
            run.duplicate_quotes,
            run.rejected_quotes,
            run.error_code,
            run.error_message,
        )

    @staticmethod
    def _from_row(row: Mapping[str, object]) -> IngestionRun:
        return IngestionRun(
            id=str(row["id"]),
            provider=str(row["provider"]),
            status=RunStatus(str(row["status"])),
            started_at=parse_timestamp(row["started_at"]),
            finished_at=_optional_time(row["finished_at"]),
            requested_symbols=_json_strings(row["requested_symbols_json"]),
            imported_quotes=int(row["imported_quotes"]),
            duplicate_quotes=int(row["duplicate_quotes"]),
            rejected_quotes=int(row["rejected_quotes"]),
            error_code=str(row["error_code"]) if row["error_code"] is not None else None,
            error_message=(
                str(row["error_message"]) if row["error_message"] is not None else None
            ),
        )


@dataclass
class AlertRepository:
    database: Database

    def save_rule(
        self,
        rule: AlertRule,
        *,
        transaction: Optional[Transaction] = None,
    ) -> AlertRule:
        if transaction is None:
            with self.database.transaction(write=True) as active:
                return self.save_rule(rule, transaction=active)
        transaction.execute(
            """
            INSERT INTO alert_rules(
                id, name, symbol, metric, operator, threshold, cooldown_seconds,
                enabled, created_at, updated_at, last_triggered_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                symbol=excluded.symbol,
                metric=excluded.metric,
                operator=excluded.operator,
                threshold=excluded.threshold,
                cooldown_seconds=excluded.cooldown_seconds,
                enabled=excluded.enabled,
                updated_at=excluded.updated_at,
                last_triggered_at=excluded.last_triggered_at,
                metadata_json=excluded.metadata_json
            """,
            (
                rule.id,
                rule.name,
                rule.symbol,
                rule.metric,
                rule.operator.value,
                str(rule.threshold),
                rule.cooldown_seconds,
                int(rule.enabled),
                format_timestamp(rule.created_at),
                format_timestamp(rule.updated_at),
                format_timestamp(rule.last_triggered_at) if rule.last_triggered_at else None,
                _json(rule.metadata),
            ),
        )
        return rule

    def get_rule(self, rule_id: str) -> Optional[AlertRule]:
        with self.database.transaction() as transaction:
            row = transaction.fetch_one("SELECT * FROM alert_rules WHERE id = ?", (rule_id,))
            return self._rule_from_row(row) if row else None

    def rules_for(self, symbol: str, *, enabled_only: bool = True) -> Tuple[AlertRule, ...]:
        with self.database.transaction() as transaction:
            sql = "SELECT * FROM alert_rules WHERE symbol = ?"
            if enabled_only:
                sql += " AND enabled = 1"
            sql += " ORDER BY created_at, id"
            return tuple(
                self._rule_from_row(row) for row in transaction.fetch_all(sql, (symbol,))
            )

    def add_alert(
        self,
        alert: Alert,
        *,
        transaction: Optional[Transaction] = None,
    ) -> Alert:
        if transaction is None:
            with self.database.transaction(write=True) as active:
                return self.add_alert(alert, transaction=active)
        try:
            transaction.execute(
                """
                INSERT INTO alerts(
                    id, rule_id, symbol, metric, observed_value, threshold,
                    operator, triggered_at, message, status, delivered_at,
                    acknowledged_at, delivery_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    alert.id,
                    alert.rule_id,
                    alert.symbol,
                    alert.metric,
                    str(alert.observed_value),
                    str(alert.threshold),
                    alert.operator.value,
                    format_timestamp(alert.triggered_at),
                    alert.message,
                    alert.status.value,
                    format_timestamp(alert.delivered_at) if alert.delivered_at else None,
                    format_timestamp(alert.acknowledged_at) if alert.acknowledged_at else None,
                    alert.delivery_error,
                ),
            )
        except StorageError as error:
            if isinstance(error.__cause__, sqlite3.IntegrityError):
                raise StorageConflictError("alert could not be inserted") from error
            raise
        return alert

    def pending(self, limit: int = 100) -> Tuple[Alert, ...]:
        with self.database.transaction() as transaction:
            rows = transaction.fetch_all(
                """
                SELECT * FROM alerts WHERE status = ?
                ORDER BY triggered_at, id LIMIT ?
                """,
                (AlertStatus.PENDING.value, limit),
            )
            return tuple(self._alert_from_row(row) for row in rows)

    def update_alert(
        self,
        alert: Alert,
        *,
        transaction: Optional[Transaction] = None,
    ) -> Alert:
        if transaction is None:
            with self.database.transaction(write=True) as active:
                return self.update_alert(alert, transaction=active)
        cursor = transaction.execute(
            """
            UPDATE alerts SET status=?, delivered_at=?, acknowledged_at=?, delivery_error=?
            WHERE id=?
            """,
            (
                alert.status.value,
                format_timestamp(alert.delivered_at) if alert.delivered_at else None,
                format_timestamp(alert.acknowledged_at) if alert.acknowledged_at else None,
                alert.delivery_error,
                alert.id,
            ),
        )
        if cursor.rowcount != 1:
            raise NotFoundError("alert was not found", details={"id": alert.id})
        return alert

    @staticmethod
    def _rule_from_row(row: Mapping[str, object]) -> AlertRule:
        return AlertRule(
            id=str(row["id"]),
            name=str(row["name"]),
            symbol=str(row["symbol"]),
            metric=str(row["metric"]),
            operator=AlertOperator(str(row["operator"])),
            threshold=Decimal(str(row["threshold"])),
            cooldown_seconds=int(row["cooldown_seconds"]),
            enabled=bool(row["enabled"]),
            created_at=parse_timestamp(row["created_at"]),
            updated_at=parse_timestamp(row["updated_at"]),
            last_triggered_at=_optional_time(row["last_triggered_at"]),
            metadata=_json_object(row["metadata_json"]),
        )

    @staticmethod
    def _alert_from_row(row: Mapping[str, object]) -> Alert:
        return Alert(
            id=str(row["id"]),
            rule_id=str(row["rule_id"]),
            symbol=str(row["symbol"]),
            metric=str(row["metric"]),
            observed_value=Decimal(str(row["observed_value"])),
            threshold=Decimal(str(row["threshold"])),
            operator=AlertOperator(str(row["operator"])),
            triggered_at=parse_timestamp(row["triggered_at"]),
            message=str(row["message"]),
            status=AlertStatus(str(row["status"])),
            delivered_at=_optional_time(row["delivered_at"]),
            acknowledged_at=_optional_time(row["acknowledged_at"]),
            delivery_error=(
                str(row["delivery_error"]) if row["delivery_error"] is not None else None
            ),
        )


@dataclass
class RepositorySet:
    """Convenient repository bundle sharing one Database."""

    database: Database

    def __post_init__(self) -> None:
        self.instruments = InstrumentRepository(self.database)
        self.quotes = QuoteRepository(self.database)
        self.candles = CandleRepository(self.database)
        self.runs = IngestionRunRepository(self.database)
        self.alerts = AlertRepository(self.database)
