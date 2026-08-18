"""Idempotent provider-to-database ingestion service."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Mapping, Sequence, Tuple

from forex_monitor.errors import ForexMonitorError, IngestionError, ProviderError, StorageError
from forex_monitor.ids import IdentifierFactory, uuid_hex
from forex_monitor.models import IngestionRun, Instrument, Quote, RunStatus, ensure_utc
from forex_monitor.providers.base import MarketDataProvider, normalize_symbols
from forex_monitor.storage import RepositorySet


@dataclass(frozen=True)
class IngestionOutcome:
    """Observable result of one ingestion attempt."""

    run: IngestionRun
    imported: Tuple[Quote, ...] = ()
    duplicates: Tuple[Quote, ...] = ()
    missing_symbols: Tuple[str, ...] = ()

    @property
    def successful(self) -> bool:
        return self.run.status in {RunStatus.SUCCEEDED, RunStatus.PARTIAL}

    def as_dict(self) -> Mapping[str, object]:
        return {
            "run": self.run.as_dict(),
            "imported": [quote.as_dict() for quote in self.imported],
            "duplicates": [quote.as_dict() for quote in self.duplicates],
            "missingSymbols": list(self.missing_symbols),
        }


@dataclass
class IngestionService:
    """Fetch, validate, audit, and atomically persist quote batches."""

    provider: MarketDataProvider
    repositories: RepositorySet
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))
    identifiers: IdentifierFactory = uuid_hex
    auto_register_instruments: bool = True
    require_all_symbols: bool = False

    def ingest(self, symbols: Sequence[str]) -> IngestionOutcome:
        requested = normalize_symbols(symbols)
        started_at = ensure_utc(self.clock(), "ingestion start")
        run = IngestionRun(
            id=self.identifiers(),
            provider=self.provider.name,
            started_at=started_at,
            requested_symbols=requested,
        )
        self.repositories.runs.create(run)
        try:
            quotes = self.provider.fetch_quotes(requested)
            self._validate_batch(quotes, requested)
            finished_at = ensure_utc(self.clock(), "ingestion finish")
            return self._commit(run, quotes, requested, finished_at)
        except (ProviderError, IngestionError, StorageError, ValueError) as error:
            self._record_failure(run, error)
            if isinstance(error, ForexMonitorError):
                raise
            raise IngestionError("ingestion failed") from error

    def _validate_batch(self, quotes: Sequence[Quote], requested: Sequence[str]) -> None:
        seen = set()
        requested_set = set(requested)
        for quote in quotes:
            if quote.provider != self.provider.name:
                raise IngestionError(
                    "quote provider does not match ingestion provider",
                    details={"symbol": quote.symbol},
                )
            if quote.symbol not in requested_set:
                raise IngestionError(
                    "provider returned an unrequested symbol",
                    details={"symbol": quote.symbol},
                )
            if quote.symbol in seen:
                raise IngestionError(
                    "provider returned duplicate symbols in one batch",
                    details={"symbol": quote.symbol},
                )
            seen.add(quote.symbol)
        missing = requested_set - seen
        if self.require_all_symbols and missing:
            raise IngestionError(
                "provider response is missing requested symbols",
                details={"symbols": sorted(missing)},
            )

    def _commit(
        self,
        run: IngestionRun,
        quotes: Sequence[Quote],
        requested: Sequence[str],
        finished_at: datetime,
    ) -> IngestionOutcome:
        imported: list[Quote] = []
        duplicates: list[Quote] = []
        with self.repositories.database.transaction(write=True) as transaction:
            if self.auto_register_instruments:
                for quote in quotes:
                    existing = self.repositories.instruments.get(
                        quote.symbol,
                        transaction=transaction,
                    )
                    if existing is None:
                        self.repositories.instruments.upsert(
                            Instrument.from_symbol(quote.symbol),
                            now=finished_at,
                            transaction=transaction,
                        )
            for quote in quotes:
                inserted = self.repositories.quotes.add(
                    quote,
                    received_at=finished_at,
                    transaction=transaction,
                )
                (imported if inserted else duplicates).append(quote)
            returned = {quote.symbol for quote in quotes}
            missing = tuple(symbol for symbol in requested if symbol not in returned)
            status = RunStatus.PARTIAL if missing else RunStatus.SUCCEEDED
            completed = run.finish(
                status=status,
                finished_at=finished_at,
                imported_quotes=len(imported),
                duplicate_quotes=len(duplicates),
                rejected_quotes=len(missing),
            )
            self.repositories.runs.update(completed, transaction=transaction)
        return IngestionOutcome(
            run=completed,
            imported=tuple(imported),
            duplicates=tuple(duplicates),
            missing_symbols=missing,
        )

    def _record_failure(self, run: IngestionRun, error: BaseException) -> None:
        finished_at = ensure_utc(self.clock(), "ingestion failure time")
        code = error.code if isinstance(error, ForexMonitorError) else "unexpected_error"
        failed = run.finish(
            status=RunStatus.FAILED,
            finished_at=finished_at,
            error_code=code,
            error_message=str(error)[:1000],
        )
        try:
            self.repositories.runs.update(failed)
        except StorageError as storage_error:
            raise IngestionError(
                "ingestion and failure audit both failed",
                details={"originalError": str(error)},
            ) from storage_error

    def seed_instruments(self, symbols: Sequence[str]) -> Tuple[Instrument, ...]:
        """Register configured instruments without fetching external data."""

        now = ensure_utc(self.clock())
        result = []
        with self.repositories.database.transaction(write=True) as transaction:
            for symbol in normalize_symbols(symbols):
                instrument = Instrument.from_symbol(symbol)
                self.repositories.instruments.upsert(
                    instrument,
                    now=now,
                    transaction=transaction,
                )
                result.append(instrument)
        return tuple(result)
