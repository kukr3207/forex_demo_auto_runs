from datetime import datetime, timedelta, timezone
import unittest

from forex_monitor.errors import StorageConflictError, ValidationError
from forex_monitor.fix.codec import FixCodec, FixMessage
from forex_monitor.fix.dictionary import FixDataType, FixDictionary, FixField
from forex_monitor.fix.session import FixSession, SessionState, fix_timestamp
from forex_monitor.fix.store import InMemoryMessageStore, StoredFixMessage


NOW = datetime(2026, 4, 2, 12, tzinfo=timezone.utc)


def message(message_type="D", sequence=1, sender="CLIENT", target="BROKER", fields=None):
    values = fields or (
        (11, "client-1"), (21, "1"), (38, "100000"), (40, "1"),
        (54, "1"), (55, "EURUSD"), (60, fix_timestamp(NOW)),
    )
    return FixMessage(message_type, sequence, sender, target, fix_timestamp(NOW), values)


class DictionaryTests(unittest.TestCase):
    def test_field_types_and_enumerations(self):
        FixField(54, "Side", FixDataType.CHAR, {"1": "Buy"}).validate("1")
        with self.assertRaises(ValidationError):
            FixField(54, "Side", FixDataType.CHAR, {"1": "Buy"}).validate("2")
        with self.assertRaises(ValidationError):
            FixField(38, "OrderQty", FixDataType.QTY).validate("not-a-number")

    def test_forex_dictionary_validates_orders(self):
        dictionary = FixDictionary.forex()
        self.assertEqual(dictionary.message("D").name, "NewOrderSingle")
        self.assertEqual(dictionary.validate("D", {35: "D", 11: "id"})[0], "required field 21 is missing")


class CodecTests(unittest.TestCase):
    def test_round_trip_body_length_and_checksum(self):
        codec = FixCodec()
        raw = codec.encode(message())
        self.assertIn(b"8=FIX.4.4\x019=", raw)
        self.assertEqual(codec.decode(raw), message())

    def test_checksum_and_body_tampering_are_rejected(self):
        codec = FixCodec()
        raw = codec.encode(message())
        with self.assertRaises(ValidationError):
            codec.decode(raw.replace(b"EURUSD", b"GBPUSD"))
        with self.assertRaises(ValidationError):
            codec.decode(raw.replace(b"9=", b"9=1", 1))

    def test_stream_decoder_handles_fragments_and_multiple_messages(self):
        codec = FixCodec()
        first = codec.encode(message(sequence=1))
        second = codec.encode(message(sequence=2, fields=(
            (11, "client-2"), (21, "1"), (38, "1"), (40, "1"),
            (54, "2"), (55, "EURUSD"), (60, fix_timestamp(NOW)),
        )))
        self.assertEqual(codec.feed(first[:10]), ())
        values = codec.feed(first[10:] + second)
        self.assertEqual(tuple(item.sequence_number for item in values), (1, 2))


class StoreTests(unittest.TestCase):
    def test_contiguous_storage_and_resend_ranges(self):
        codec = FixCodec()
        store = InMemoryMessageStore()
        first = message(sequence=1)
        stored = StoredFixMessage("outbound", 1, first, codec.encode(first), NOW)
        self.assertIs(store.save(stored), store.save(stored))
        self.assertEqual(store.range("outbound", 1), (stored,))
        with self.assertRaises(StorageConflictError):
            second = message(sequence=3)
            store.save(StoredFixMessage("outbound", 3, second, codec.encode(second), NOW))


class SessionTests(unittest.TestCase):
    def peer(self, message_type, sequence, fields=()):
        return FixCodec().encode(FixMessage(
            message_type, sequence, "BROKER", "CLIENT", fix_timestamp(NOW), fields,
        ))

    def test_logon_application_heartbeat_and_logout(self):
        session = FixSession("CLIENT", "BROKER", heartbeat_interval=30)
        outbound = session.logon(NOW, username="user", password="secret")
        self.assertEqual(session.state, SessionState.LOGON_SENT)
        self.assertEqual(FixCodec().decode(outbound).message_type, "A")
        session.receive(self.peer("A", 1, ((98, "0"), (108, "30"))), NOW)
        self.assertEqual(session.state, SessionState.ACTIVE)
        order_raw = session.application("D", message().fields, NOW + timedelta(seconds=1))
        self.assertEqual(FixCodec().decode(order_raw).sequence_number, 2)
        self.assertTrue(session.heartbeat_due(NOW + timedelta(seconds=31)))
        session.heartbeat(NOW + timedelta(seconds=31))
        session.logout(NOW + timedelta(seconds=32), "done")
        self.assertEqual(session.state, SessionState.LOGOUT_SENT)

    def test_sequence_gaps_are_detected_without_advancing_store(self):
        session = FixSession("CLIENT", "BROKER")
        session.logon(NOW)
        with self.assertRaises(StorageConflictError):
            session.receive(self.peer("A", 2, ((98, "0"), (108, "30"))), NOW)
        self.assertEqual(session.store.next_inbound, 1)

    def test_test_request_receives_matching_heartbeat(self):
        session = FixSession("CLIENT", "BROKER")
        session.logon(NOW)
        session.receive(self.peer("A", 1, ((98, "0"), (108, "30"))), NOW)
        session.test_request("probe", NOW + timedelta(seconds=1))
        self.assertEqual(session.test_request_id, "probe")
        session.receive(self.peer("0", 2, ((112, "probe"),)), NOW + timedelta(seconds=2))
        self.assertIsNone(session.test_request_id)


if __name__ == "__main__":
    unittest.main()
