"""Command-line interface for administration, ingestion, and reporting."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from forex_monitor.analytics.aggregation import aggregate_quotes
from forex_monitor.application import Application, build_application
from forex_monitor.config import AppConfig
from forex_monitor.errors import ForexMonitorError
from forex_monitor.models import Timeframe, parse_timestamp
from forex_monitor.reporting import ReportService, compact_json, pretty_json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="forex-monitor",
        description="Ingest, analyze, alert on, and export foreign-exchange market data.",
    )
    parser.add_argument(
        "--database",
        type=Path,
        help="SQLite database path (overrides FOREX_DATABASE_PATH)",
    )
    parser.add_argument("--pretty", action="store_true", help="pretty-print JSON output")
    parser.add_argument("--verbose", action="count", default=0, help="increase log verbosity")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init", help="initialize the database and configured instruments")
    subparsers.add_parser("status", help="show provider, database, and recent-run status")

    ingest = subparsers.add_parser("ingest", help="fetch and persist one quote batch")
    ingest.add_argument("symbols", nargs="*", help="symbols; defaults to configured instruments")

    quotes = subparsers.add_parser("quotes", help="list stored quote history")
    quotes.add_argument("symbol")
    quotes.add_argument("--start", type=_timestamp)
    quotes.add_argument("--end", type=_timestamp)
    quotes.add_argument("--provider")
    quotes.add_argument("--limit", type=_positive_int, default=100)
    quotes.add_argument("--descending", action="store_true")

    aggregate = subparsers.add_parser("aggregate", help="build and persist candles from quotes")
    aggregate.add_argument("symbol")
    aggregate.add_argument("timeframe", type=_timeframe)
    aggregate.add_argument("--start", type=_timestamp)
    aggregate.add_argument("--end", type=_timestamp)
    aggregate.add_argument("--limit", type=_positive_int, default=10_000)
    aggregate.add_argument("--fill-gaps", action="store_true")

    candles = subparsers.add_parser("candles", help="list stored candles")
    candles.add_argument("symbol")
    candles.add_argument("timeframe", type=_timeframe)
    candles.add_argument("--start", type=_timestamp)
    candles.add_argument("--end", type=_timestamp)
    candles.add_argument("--limit", type=_positive_int, default=500)

    analyze = subparsers.add_parser("analyze", help="calculate indicators and a signal")
    analyze.add_argument("symbol")
    analyze.add_argument("timeframe", type=_timeframe)
    analyze.add_argument("--limit", type=_positive_int, default=200)

    report = subparsers.add_parser("report", help="generate a market overview report")
    report.add_argument("--format", choices=("json", "markdown"), default="json")
    report.add_argument("--output", type=Path)
    report.add_argument("--history-limit", type=_positive_int, default=200)
    report.add_argument("--run-limit", type=_positive_int, default=20)

    export = subparsers.add_parser("export", help="export quote history")
    export.add_argument("symbol")
    export.add_argument("output", type=Path)
    export.add_argument("--format", choices=("csv", "json", "jsonl"), default="csv")
    export.add_argument("--start", type=_timestamp)
    export.add_argument("--end", type=_timestamp)
    export.add_argument("--limit", type=_positive_int, default=100_000)
    return parser


def _positive_int(value: str) -> int:
    try:
        result = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if result < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return result


def _timestamp(value: str) -> datetime:
    try:
        return parse_timestamp(value)
    except ForexMonitorError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _timeframe(value: str) -> Timeframe:
    try:
        return Timeframe.parse(value)
    except ForexMonitorError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _config(args: argparse.Namespace) -> AppConfig:
    environ = dict(os.environ)
    if args.database:
        environ["FOREX_DATABASE_PATH"] = str(args.database)
    return AppConfig.from_environment(environ)


def _logging(verbose: int, configured_level: str) -> None:
    level = logging.DEBUG if verbose >= 2 else logging.INFO if verbose == 1 else configured_level
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _emit(value: object, *, pretty: bool) -> None:
    sys.stdout.write(pretty_json(value) if pretty else compact_json(value))


class RawOutput(str):
    """Marker for already serialized report output."""


def dispatch(args: argparse.Namespace, application: Application) -> object:
    if args.command == "init":
        version = application.initialize()
        return {
            "success": True,
            "schemaVersion": version,
            "instruments": [item.as_dict() for item in application.repositories.instruments.list()],
        }
    application.initialize()
    if args.command == "status":
        health = application.provider.health()
        return {
            "success": True,
            "database": application.database.health(),
            "provider": {
                "name": health.provider,
                "healthy": health.healthy,
                "checkedAt": health.checked_at.isoformat(),
                "latencySeconds": health.latency_seconds,
                "message": health.message,
            },
            "recentRuns": [run.as_dict() for run in application.repositories.runs.recent(10)],
        }
    if args.command == "ingest":
        symbols = args.symbols or application.config.runtime.instruments
        return {"success": True, **application.ingestion.ingest(symbols).as_dict()}
    if args.command == "quotes":
        quotes = application.repositories.quotes.range(
            args.symbol,
            start=args.start,
            end=args.end,
            provider=args.provider,
            limit=args.limit,
            ascending=not args.descending,
        )
        return {"success": True, "count": len(quotes), "quotes": [q.as_dict() for q in quotes]}
    if args.command == "aggregate":
        quotes = application.repositories.quotes.range(
            args.symbol,
            start=args.start,
            end=args.end,
            limit=args.limit,
        )
        created = aggregate_quotes(quotes, args.timeframe, include_empty=args.fill_gaps)
        now = datetime.now(timezone.utc)
        with application.database.transaction(write=True) as transaction:
            for candle in created:
                application.repositories.candles.upsert(candle, now=now, transaction=transaction)
        return {"success": True, "count": len(created), "candles": [c.as_dict() for c in created]}
    if args.command == "candles":
        candles = application.repositories.candles.range(
            args.symbol,
            args.timeframe,
            start=args.start,
            end=args.end,
            limit=args.limit,
        )
        return {
            "success": True,
            "count": len(candles),
            "candles": [candle.as_dict() for candle in candles],
        }
    if args.command == "analyze":
        analysis = application.analysis.analyze(args.symbol, args.timeframe, limit=args.limit)
        return {"success": True, "analysis": analysis.as_dict()}
    reports = ReportService(application.repositories)
    if args.command == "report":
        report = reports.market_report(history_limit=args.history_limit, run_limit=args.run_limit)
        text = report.to_markdown() if args.format == "markdown" else report.to_json(pretty=args.pretty)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
            return {"success": True, "output": str(args.output), "format": args.format}
        return RawOutput(text)
    if args.command == "export":
        output = reports.export_quotes(
            args.symbol,
            output=args.output,
            format=args.format,
            start=args.start,
            end=args.end,
            limit=args.limit,
        )
        return {"success": True, "output": str(output), "format": args.format}
    raise ValueError(f"unsupported command: {args.command}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = _config(args)
        _logging(args.verbose, config.runtime.log_level)
        result = dispatch(args, build_application(config))
        if isinstance(result, RawOutput):
            sys.stdout.write(str(result))
        else:
            _emit(result, pretty=args.pretty)
        return 0
    except ForexMonitorError as error:
        _emit(error.as_dict(), pretty=args.pretty)
        return 2
    except KeyboardInterrupt:
        _emit({"error": "interrupted", "code": "interrupted"}, pretty=args.pretty)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
