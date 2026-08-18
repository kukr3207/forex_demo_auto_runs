from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from forex_monitor.errors import ValidationError
from forex_monitor.models import (
    Alert,
    AlertOperator,
    AlertRule,
    AlertStatus,
    AnalysisResult,
    Candle,
    IndicatorValue,
    IngestionRun,
    Instrument,
    MarketSnapshot,
    Quote,
    RunStatus,
    Signal,
    Timeframe,
    as_decimal,
    common_symbol,
    ensure_utc,
    format_timestamp,
    parse_timestamp,
    validate_quote_sequence,
)
from tests.support import BASE_TIME, candle, quote


class TimestampTests(unittest.TestCase):
    def test_parse_z_timestamp(self) -> None:
        parsed = parse_timestamp("2026-01-01T10:20:30.123456Z")
        self.assertEqual(parsed.tzinfo, timezone.utc)
        self.assertEqual(format_timestamp(parsed), "2026-01-01T10:20:30.123456Z")

    def test_parse_offset_normalizes_to_utc(self) -> None:
        parsed = parse_timestamp("2026-01-01T15:50:30+05:30")
        self.assertEqual(parsed, datetime(2026, 1, 1, 10, 20, 30, tzinfo=timezone.utc))

    def test_naive_datetime_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValidationError, "timezone"):
            ensure_utc(datetime(2026, 1, 1))

    def test_invalid_timestamp_text_is_rejected(self) -> None:
        for value in ("", "yesterday", "2026-99-99", None, 123):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                parse_timestamp(value)


class DecimalTests(unittest.TestCase):
    def test_decimal_from_float_uses_string_form(self) -> None:
        self.assertEqual(as_decimal(1.2, "value"), Decimal("1.2"))

    def test_non_finite_values_are_rejected(self) -> None:
        for value in (float("nan"), float("inf"), "NaN", "Infinity"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                as_decimal(value, "value")

    def test_positive_rule_is_strict(self) -> None:
        for value in (0, "0", -1, Decimal("-0.1")):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                as_decimal(value, "value", positive=True)


class TimeframeTests(unittest.TestCase):
    def test_seconds_mapping(self) -> None:
        expected = {
            "1m": 60,
            "5m": 300,
            "15m": 900,
            "30m": 1800,
            "1h": 3600,
            "4h": 14_400,
            "1d": 86_400,
        }
        self.assertEqual({item.value: item.seconds for item in Timeframe}, expected)

    def test_parse_is_case_insensitive(self) -> None:
        self.assertIs(Timeframe.parse(" 1H "), Timeframe.HOUR_1)

    def test_unknown_timeframe_is_rejected(self) -> None:
        for value in ("2h", "day", 60, None):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                Timeframe.parse(value)


class InstrumentTests(unittest.TestCase):
    def test_from_symbol_infers_jpy_precision(self) -> None:
        instrument = Instrument.from_symbol("usd/jpy")
        self.assertEqual(instrument.symbol, "USDJPY")
        self.assertEqual(instrument.precision, 3)
        self.assertEqual(instrument.pip_size, Decimal("0.01"))

    def test_from_symbol_infers_standard_precision(self) -> None:
        instrument = Instrument.from_symbol("EURUSD")
        self.assertEqual(instrument.base_currency, "EUR")
        self.assertEqual(instrument.quote_currency, "USD")
        self.assertEqual(instrument.display_name, "EUR/USD")

    def test_invalid_currency_pair_is_rejected(self) -> None:
        for symbol in ("EUR", "EURUS1", "EUR-EUR", ""):
            with self.subTest(symbol=symbol), self.assertRaises(ValidationError):
                Instrument.from_symbol(symbol)

    def test_same_base_and_quote_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValidationError, "must differ"):
            Instrument("USDUSD", "USD", "USD")

    def test_metadata_is_copied(self) -> None:
        source = {"venue": "demo"}
        instrument = Instrument("EURUSD", "EUR", "USD", metadata=source)
        source["venue"] = "changed"
        self.assertEqual(instrument.metadata["venue"], "demo")


class QuoteTests(unittest.TestCase):
    def test_mid_and_spread(self) -> None:
        value = quote(bid="1.1000", ask="1.1004")
        self.assertEqual(value.mid, Decimal("1.1002"))
        self.assertEqual(value.spread, Decimal("0.0004"))

    def test_spread_pips_validates_symbol(self) -> None:
        value = quote(bid="1.1000", ask="1.1004")
        self.assertEqual(value.spread_pips(Instrument.from_symbol("EURUSD")), Decimal("4"))
        with self.assertRaises(ValidationError):
            value.spread_pips(Instrument.from_symbol("GBPUSD"))

    def test_ask_below_bid_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValidationError, "ask"):
            quote(bid="1.2", ask="1.1")

    def test_quote_mapping_round_trip(self) -> None:
        original = quote()
        restored = Quote.from_mapping(original.as_dict())
        self.assertEqual(restored, original)

    def test_negative_volume_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            quote(volume="-1")


class CandleTests(unittest.TestCase):
    def test_properties_for_bullish_candle(self) -> None:
        value = candle(change="0.002")
        self.assertEqual(value.direction, 1)
        self.assertEqual(value.body, Decimal("0.002"))
        self.assertGreater(value.range, value.body)

    def test_properties_for_bearish_and_flat_candles(self) -> None:
        self.assertEqual(candle(change="-0.002").direction, -1)
        self.assertEqual(candle(change="0").direction, 0)

    def test_invalid_high_and_low_are_rejected(self) -> None:
        base = candle()
        with self.assertRaises(ValidationError):
            replace(base, high=base.open - Decimal("0.1"))
        with self.assertRaises(ValidationError):
            replace(base, low=base.open + Decimal("0.1"))

    def test_close_must_follow_open(self) -> None:
        base = candle()
        with self.assertRaises(ValidationError):
            replace(base, closed_at=base.opened_at)


class SnapshotTests(unittest.TestCase):
    def test_quotes_are_sorted_by_symbol(self) -> None:
        first = quote("GBPUSD", provider="fixture")
        second = quote("EURUSD", provider="fixture", source_id="source_2")
        snapshot = MarketSnapshot(
            "snapshot_1",
            "fixture",
            BASE_TIME,
            (first, second),
            BASE_TIME,
            "a" * 64,
        )
        self.assertEqual([item.symbol for item in snapshot.quotes], ["EURUSD", "GBPUSD"])
        self.assertEqual(snapshot.quote_for("eur/usd"), second)

    def test_duplicate_symbols_are_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            MarketSnapshot(
                "snapshot_1",
                "fixture",
                BASE_TIME,
                (quote(), quote(source_id="source_2")),
                BASE_TIME,
                "a" * 64,
            )

    def test_invalid_checksum_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValidationError, "checksum"):
            MarketSnapshot("snapshot_1", "fixture", BASE_TIME, (quote(),), BASE_TIME, "nope")


class AlertModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rule = AlertRule(
            "rule_1",
            "High mid",
            "EURUSD",
            "mid",
            AlertOperator.GREATER_THAN,
            Decimal("1.2"),
            created_at=BASE_TIME,
            updated_at=BASE_TIME,
        )

    def test_with_trigger_updates_times(self) -> None:
        later = BASE_TIME + timedelta(hours=1)
        updated = self.rule.with_trigger(later)
        self.assertEqual(updated.last_triggered_at, later)
        self.assertEqual(updated.updated_at, later)
        self.assertIsNone(self.rule.last_triggered_at)

    def test_rule_operator_parser(self) -> None:
        for operator in AlertOperator:
            self.assertIs(AlertOperator.parse(operator.value), operator)

    def test_alert_requires_consistent_dates(self) -> None:
        with self.assertRaises(ValidationError):
            Alert(
                "alert_1",
                self.rule.id,
                "EURUSD",
                "mid",
                Decimal("1.3"),
                self.rule.threshold,
                self.rule.operator,
                BASE_TIME,
                "message",
                status=AlertStatus.DELIVERED,
                delivered_at=BASE_TIME - timedelta(seconds=1),
            )


class AnalysisAndRunTests(unittest.TestCase):
    def test_analysis_indicator_lookup(self) -> None:
        indicator = IndicatorValue(
            "EURUSD", "RSI", Timeframe.HOUR_1, BASE_TIME, Decimal("55")
        )
        analysis = AnalysisResult(
            "EURUSD",
            Timeframe.HOUR_1,
            BASE_TIME,
            (indicator,),
            signal=Signal.BUY,
            score=Decimal("25"),
        )
        self.assertEqual(analysis.value("rsi"), Decimal("55"))
        self.assertIsNone(analysis.value("missing"))

    def test_run_finish_is_immutable(self) -> None:
        run = IngestionRun("run_1", "fixture", BASE_TIME, requested_symbols=("EURUSD",))
        finished = run.finish(
            status=RunStatus.SUCCEEDED,
            finished_at=BASE_TIME + timedelta(seconds=2),
            imported_quotes=1,
        )
        self.assertIsNone(run.finished_at)
        self.assertEqual(finished.duration_seconds, 2)
        self.assertEqual(finished.imported_quotes, 1)

    def test_running_status_cannot_finish(self) -> None:
        run = IngestionRun("run_1", "fixture", BASE_TIME)
        with self.assertRaises(ValidationError):
            run.finish(status=RunStatus.RUNNING, finished_at=BASE_TIME)


class SequenceValidationTests(unittest.TestCase):
    def test_validate_sequence_sorts(self) -> None:
        later = quote(observed_at=BASE_TIME + timedelta(seconds=1), source_id="later")
        earlier = quote(observed_at=BASE_TIME, source_id="earlier")
        self.assertEqual(validate_quote_sequence((later, earlier)), (earlier, later))

    def test_validate_sequence_rejects_duplicates(self) -> None:
        value = quote()
        with self.assertRaises(ValidationError):
            validate_quote_sequence((value, value))

    def test_common_symbol_requires_homogeneous_data(self) -> None:
        self.assertEqual(common_symbol((quote(), quote(source_id="second"))), "EURUSD")
        with self.assertRaises(ValidationError):
            common_symbol((quote(), quote("GBPUSD", source_id="second")))
        with self.assertRaises(ValidationError):
            common_symbol(())


if __name__ == "__main__":
    unittest.main()

