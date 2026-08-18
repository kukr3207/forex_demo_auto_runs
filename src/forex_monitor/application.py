"""Application composition root.

Only this module chooses concrete providers and storage. Domain services remain
portable and can be constructed directly by tests or embedding applications.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from forex_monitor.alerts import AlertService, ConsoleNotifier
from forex_monitor.analytics import AnalysisService
from forex_monitor.config import AppConfig
from forex_monitor.ingestion import IngestionOutcome, IngestionService
from forex_monitor.models import Instrument
from forex_monitor.providers import FxssiNormalizer, HttpJsonProvider, MarketDataProvider
from forex_monitor.storage import Database, RepositorySet


@dataclass
class Application:
    config: AppConfig
    database: Database
    repositories: RepositorySet
    provider: MarketDataProvider
    ingestion: IngestionService
    analysis: AnalysisService
    alerts: AlertService

    def initialize(self) -> int:
        version = self.database.initialize()
        self.ensure_configured_instruments()
        return version

    def ensure_configured_instruments(self) -> tuple[Instrument, ...]:
        now = datetime.now(timezone.utc)
        instruments = []
        with self.database.transaction(write=True) as transaction:
            for symbol in self.config.runtime.instruments:
                instrument = Instrument.from_symbol(symbol)
                self.repositories.instruments.upsert(
                    instrument,
                    now=now,
                    transaction=transaction,
                )
                instruments.append(instrument)
        return tuple(instruments)

    def ingest_configured(self) -> IngestionOutcome:
        return self.ingestion.ingest(self.config.runtime.instruments)


def build_application(
    config: Optional[AppConfig] = None,
    *,
    provider: Optional[MarketDataProvider] = None,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> Application:
    """Build the production object graph with optional deterministic overrides."""

    settings = config or AppConfig.from_environment()
    database = Database(settings.database)
    repositories = RepositorySet(database)
    selected_provider = provider or HttpJsonProvider(
        settings.provider,
        FxssiNormalizer(provider_name=settings.provider.name),
        clock=clock,
    )
    ingestion = IngestionService(
        selected_provider,
        repositories,
        clock=clock,
        require_all_symbols=False,
    )
    analysis = AnalysisService(repositories.candles)
    alerts = AlertService(
        database,
        repositories.alerts,
        ConsoleNotifier(clock=clock),
        clock=clock,
    )
    return Application(
        config=settings,
        database=database,
        repositories=repositories,
        provider=selected_provider,
        ingestion=ingestion,
        analysis=analysis,
        alerts=alerts,
    )
