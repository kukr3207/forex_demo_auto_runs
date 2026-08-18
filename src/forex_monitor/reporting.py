"""Deterministic JSON, CSV, and Markdown market reports."""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence, Tuple

from forex_monitor.analytics.performance import simple_returns, summarize_returns
from forex_monitor.errors import ReportError
from forex_monitor.models import Candle, Quote, format_timestamp
from forex_monitor.storage import RepositorySet


def compact_json(value: object, *, trailing_newline: bool = True) -> str:
    """Serialize JSON deterministically without ASCII escaping."""

    try:
        text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as error:
        raise ReportError("report contains a value that is not JSON serializable") from error
    return text + ("\n" if trailing_newline else "")


def pretty_json(value: object, *, trailing_newline: bool = True) -> str:
    try:
        text = json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False)
    except (TypeError, ValueError) as error:
        raise ReportError("report contains a value that is not JSON serializable") from error
    return text + ("\n" if trailing_newline else "")


def quotes_csv(quotes: Sequence[Quote]) -> str:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(
        ["symbol", "bid", "ask", "mid", "spread", "volume", "observed_at", "provider", "source_id"]
    )
    for quote in quotes:
        writer.writerow(
            [
                quote.symbol,
                str(quote.bid),
                str(quote.ask),
                str(quote.mid),
                str(quote.spread),
                str(quote.volume) if quote.volume is not None else "",
                format_timestamp(quote.observed_at),
                quote.provider,
                quote.source_id or "",
            ]
        )
    return stream.getvalue()


def candles_csv(candles: Sequence[Candle]) -> str:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(
        [
            "symbol",
            "timeframe",
            "opened_at",
            "closed_at",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "sample_count",
            "complete",
        ]
    )
    for candle in candles:
        writer.writerow(
            [
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
                "true" if candle.complete else "false",
            ]
        )
    return stream.getvalue()


@dataclass(frozen=True)
class MarketReport:
    generated_at: datetime
    instruments: Tuple[Mapping[str, object], ...]
    latest_quotes: Tuple[Mapping[str, object], ...]
    performance: Mapping[str, Mapping[str, object]]
    ingestion_runs: Tuple[Mapping[str, object], ...]
    database_health: Mapping[str, object]

    def as_dict(self) -> Mapping[str, object]:
        return {
            "format": "forex-monitor-report",
            "version": 1,
            "generatedAt": format_timestamp(self.generated_at),
            "database": dict(self.database_health),
            "instruments": list(self.instruments),
            "latestQuotes": list(self.latest_quotes),
            "performance": dict(self.performance),
            "ingestionRuns": list(self.ingestion_runs),
        }

    def to_json(self, *, pretty: bool = False) -> str:
        return pretty_json(self.as_dict()) if pretty else compact_json(self.as_dict())

    def to_markdown(self) -> str:
        lines = [
            "# Forex Monitor Report",
            "",
            f"Generated: `{format_timestamp(self.generated_at)}`",
            "",
            "## Latest quotes",
            "",
            "| Symbol | Bid | Ask | Mid | Spread | Observed |",
            "| --- | ---: | ---: | ---: | ---: | --- |",
        ]
        for quote in self.latest_quotes:
            lines.append(
                "| {symbol} | {bid} | {ask} | {mid} | {spread} | {observedAt} |".format(**quote)
            )
        if not self.latest_quotes:
            lines.append("| _No data_ |  |  |  |  |  |")
        lines.extend(["", "## Recent ingestion runs", ""])
        for run in self.ingestion_runs:
            lines.append(
                f"- `{run['id']}` — {run['status']} — imported {run['importedQuotes']} quote(s)"
            )
        if not self.ingestion_runs:
            lines.append("- No ingestion runs")
        lines.extend(["", "## Performance", ""])
        for symbol, summary in sorted(self.performance.items()):
            lines.append(
                f"- **{symbol}**: total return {summary['totalReturn']}, "
                f"volatility {summary['annualizedVolatility']}, "
                f"maximum drawdown {summary['maximumDrawdown']}"
            )
        if not self.performance:
            lines.append("- Insufficient history")
        return "\n".join(lines) + "\n"


@dataclass
class ReportService:
    repositories: RepositorySet
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))

    def market_report(
        self,
        *,
        history_limit: int = 200,
        run_limit: int = 20,
        periods_per_year: int = 252,
    ) -> MarketReport:
        instruments = self.repositories.instruments.list()
        latest = []
        performance = {}
        for instrument in instruments:
            quote = self.repositories.quotes.latest(instrument.symbol)
            if quote:
                latest.append(quote.as_dict())
            history = self.repositories.quotes.range(
                instrument.symbol,
                limit=history_limit,
                ascending=True,
            )
            if len(history) >= 2:
                returns = simple_returns(tuple(item.mid for item in history))
                performance[instrument.symbol] = summarize_returns(
                    returns,
                    periods_per_year=periods_per_year,
                ).as_dict()
        return MarketReport(
            generated_at=self.clock(),
            instruments=tuple(instrument.as_dict() for instrument in instruments),
            latest_quotes=tuple(latest),
            performance=performance,
            ingestion_runs=tuple(run.as_dict() for run in self.repositories.runs.recent(run_limit)),
            database_health=self.repositories.database.health(),
        )

    def export_quotes(
        self,
        symbol: str,
        *,
        output: Path,
        format: str = "csv",
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        limit: int = 100_000,
    ) -> Path:
        quotes = self.repositories.quotes.range(
            symbol,
            start=start,
            end=end,
            limit=limit,
            ascending=True,
        )
        normalized = format.strip().lower()
        if normalized == "csv":
            text = quotes_csv(quotes)
        elif normalized == "json":
            text = pretty_json([quote.as_dict() for quote in quotes])
        elif normalized == "jsonl":
            text = "".join(compact_json(quote.as_dict()) for quote in quotes)
        else:
            raise ReportError("export format must be csv, json, or jsonl")
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
        except OSError as error:
            raise ReportError("could not write report", details={"path": str(output)}) from error
        return output
