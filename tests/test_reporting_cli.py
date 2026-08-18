from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from forex_monitor.application import build_application
from forex_monitor.cli import build_parser, dispatch, main
from forex_monitor.config import AppConfig, DatabaseConfig, RuntimeConfig
from forex_monitor.models import Instrument, Timeframe
from forex_monitor.providers import FixtureProvider
from forex_monitor.reporting import (
    ReportService,
    candles_csv,
    compact_json,
    pretty_json,
    quotes_csv,
)
from tests.support import BASE_TIME, TemporaryRepositories, candle_series, quote, quote_series


class SerializationTests(unittest.TestCase):
    def test_compact_json_is_sorted_and_has_newline(self) -> None:
        self.assertEqual(compact_json({"b": 2, "a": 1}), '{"a":1,"b":2}\n')

    def test_pretty_json_is_indented(self) -> None:
        text = pretty_json({"b": 2, "a": 1})
        self.assertTrue(text.endswith("\n"))
        self.assertIn('\n  "a": 1,', text)

    def test_quotes_csv_has_stable_columns(self) -> None:
        text = quotes_csv((quote(),))
        lines = text.splitlines()
        self.assertEqual(
            lines[0], "symbol,bid,ask,mid,spread,volume,observed_at,provider,source_id"
        )
        self.assertTrue(lines[1].startswith("EURUSD,1.1000,1.1002,"))

    def test_candles_csv_has_boolean_text(self) -> None:
        text = candles_csv(candle_series(1))
        self.assertIn("sample_count,complete", text.splitlines()[0])
        self.assertTrue(text.splitlines()[1].endswith(",10,true"))


class ReportServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = TemporaryRepositories()
        self.repositories = self.context.__enter__()
        self.repositories.instruments.upsert(Instrument.from_symbol("EURUSD"), now=BASE_TIME)
        self.repositories.quotes.add_many(quote_series(5), received_at=BASE_TIME)
        self.service = ReportService(self.repositories, clock=lambda: BASE_TIME)

    def tearDown(self) -> None:
        self.context.__exit__()

    def test_market_report_contains_performance(self) -> None:
        report = self.service.market_report(periods_per_year=252)
        payload = report.as_dict()
        self.assertEqual(payload["format"], "forex-monitor-report")
        self.assertEqual(len(payload["latestQuotes"]), 1)
        self.assertIn("EURUSD", payload["performance"])
        self.assertTrue(payload["database"]["healthy"])

    def test_json_is_deterministic(self) -> None:
        report = self.service.market_report()
        self.assertEqual(report.to_json(), report.to_json())
        self.assertTrue(report.to_json().endswith("\n"))

    def test_markdown_contains_tables_and_sections(self) -> None:
        text = self.service.market_report().to_markdown()
        self.assertIn("# Forex Monitor Report", text)
        self.assertIn("| EURUSD |", text)
        self.assertIn("## Performance", text)

    def test_export_formats(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for format in ("csv", "json", "jsonl"):
                with self.subTest(format=format):
                    output = Path(directory) / f"quotes.{format}"
                    self.service.export_quotes("EURUSD", output=output, format=format)
                    self.assertTrue(output.exists())
                    self.assertTrue(output.read_text().endswith("\n"))


class ParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = build_parser()

    def test_init_command(self) -> None:
        args = self.parser.parse_args(("--database", "/tmp/test.db", "init"))
        self.assertEqual(args.command, "init")
        self.assertEqual(args.database, Path("/tmp/test.db"))

    def test_quotes_options(self) -> None:
        args = self.parser.parse_args(
            ("quotes", "EURUSD", "--limit", "25", "--provider", "fixture", "--descending")
        )
        self.assertEqual(args.limit, 25)
        self.assertEqual(args.provider, "fixture")
        self.assertTrue(args.descending)

    def test_bad_positive_integer_exits(self) -> None:
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            self.parser.parse_args(("quotes", "EURUSD", "--limit", "0"))

    def test_timeframe_parser(self) -> None:
        args = self.parser.parse_args(("candles", "EURUSD", "1h"))
        self.assertIs(args.timeframe, Timeframe.HOUR_1)


class CliIntegrationTests(unittest.TestCase):
    def test_main_init_creates_database_and_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cli.db"
            stream = io.StringIO()
            with redirect_stdout(stream):
                result = main(("--database", str(path), "init"))
            self.assertEqual(result, 0)
            payload = json.loads(stream.getvalue())
            self.assertTrue(payload["success"])
            self.assertEqual(payload["schemaVersion"], 4)
            self.assertTrue(path.exists())

    def test_dispatch_ingest_quotes_aggregate_and_list(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            values = quote_series(5)
            config = AppConfig(
                database=DatabaseConfig(path=Path(directory) / "app.db"),
                runtime=RuntimeConfig(instruments=("EURUSD",)),
            )
            application = build_application(
                config,
                provider=FixtureProvider(tuple((value,) for value in values)),
                clock=lambda: BASE_TIME,
            )
            parser = build_parser()
            for _ in values:
                ingest_result = dispatch(parser.parse_args(("ingest", "EURUSD")), application)
                self.assertTrue(ingest_result["success"])  # type: ignore[index]
            quote_result = dispatch(parser.parse_args(("quotes", "EURUSD")), application)
            self.assertEqual(quote_result["count"], 5)  # type: ignore[index]
            aggregate_result = dispatch(
                parser.parse_args(("aggregate", "EURUSD", "5m")),
                application,
            )
            self.assertEqual(aggregate_result["count"], 1)  # type: ignore[index]
            candle_result = dispatch(
                parser.parse_args(("candles", "EURUSD", "5m")),
                application,
            )
            self.assertEqual(candle_result["count"], 1)  # type: ignore[index]


if __name__ == "__main__":
    unittest.main()
