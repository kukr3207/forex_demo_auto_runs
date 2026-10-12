"""FIX session state, heartbeats, sequence checks, and administrative messages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Mapping, Optional, Tuple

from forex_monitor.errors import StorageConflictError, ValidationError
from forex_monitor.fix.codec import FixCodec, FixMessage
from forex_monitor.fix.store import InMemoryMessageStore, StoredFixMessage
from forex_monitor.models import ensure_utc


class SessionState(str, Enum):
    DISCONNECTED = "disconnected"
    LOGON_SENT = "logon_sent"
    ACTIVE = "active"
    LOGOUT_SENT = "logout_sent"
    TERMINATED = "terminated"


@dataclass(frozen=True)
class SessionEvent:
    kind: str
    occurred_at: datetime
    sequence_number: Optional[int] = None
    message_type: Optional[str] = None
    detail: Optional[str] = None


def fix_timestamp(value: datetime) -> str:
    return ensure_utc(value).strftime("%Y%m%d-%H:%M:%S.%f")[:-3]


class FixSession:
    def __init__(
        self,
        sender: str,
        target: str,
        *,
        heartbeat_interval: int = 30,
        codec: Optional[FixCodec] = None,
        store: Optional[InMemoryMessageStore] = None,
    ) -> None:
        self.sender = sender.strip()
        self.target = target.strip()
        if not self.sender or not self.target:
            raise ValidationError("FIX sender and target cannot be blank")
        if heartbeat_interval <= 0:
            raise ValidationError("FIX heartbeat interval must be positive")
        self.heartbeat_interval = heartbeat_interval
        self.codec = codec or FixCodec()
        self.store = store or InMemoryMessageStore()
        self.state = SessionState.DISCONNECTED
        self.last_sent: Optional[datetime] = None
        self.last_received: Optional[datetime] = None
        self.test_request_id: Optional[str] = None
        self.events: list[SessionEvent] = []

    def _message(self, message_type: str, at: datetime, fields: Tuple[Tuple[int, str], ...] = ()) -> FixMessage:
        return FixMessage(
            message_type,
            self.store.next_outbound,
            self.sender,
            self.target,
            fix_timestamp(at),
            fields,
            self.codec.dictionary.begin_string,
        )

    def _send(self, message: FixMessage, at: datetime) -> bytes:
        timestamp = ensure_utc(at, "FIX send time")
        raw = self.codec.encode(message)
        self.store.save(StoredFixMessage("outbound", message.sequence_number, message, raw, timestamp))
        self.last_sent = timestamp
        self.events.append(SessionEvent("sent", timestamp, message.sequence_number, message.message_type))
        return raw

    def logon(self, at: datetime, *, reset_sequence: bool = False, username: Optional[str] = None, password: Optional[str] = None) -> bytes:
        if self.state not in {SessionState.DISCONNECTED, SessionState.TERMINATED}:
            raise ValidationError("FIX session is already connected")
        if reset_sequence:
            self.store.reset()
        fields = [(98, "0"), (108, str(self.heartbeat_interval))]
        if reset_sequence:
            fields.append((141, "Y"))
        if username:
            fields.append((553, username))
        if password:
            fields.append((554, password))
        timestamp = ensure_utc(at, "FIX logon time")
        raw = self._send(self._message("A", timestamp, tuple(fields)), timestamp)
        self.state = SessionState.LOGON_SENT
        return raw

    def logout(self, at: datetime, text: Optional[str] = None) -> bytes:
        if self.state is not SessionState.ACTIVE:
            raise ValidationError("FIX session is not active")
        fields = ((58, text),) if text else ()
        timestamp = ensure_utc(at, "FIX logout time")
        raw = self._send(self._message("5", timestamp, fields), timestamp)
        self.state = SessionState.LOGOUT_SENT
        return raw

    def application(self, message_type: str, fields: Tuple[Tuple[int, str], ...], at: datetime) -> bytes:
        if self.state is not SessionState.ACTIVE:
            raise ValidationError("FIX session is not active")
        timestamp = ensure_utc(at, "FIX application time")
        return self._send(self._message(message_type, timestamp, fields), timestamp)

    def receive(self, raw: bytes, at: datetime) -> FixMessage:
        timestamp = ensure_utc(at, "FIX receive time")
        message = self.codec.decode(raw)
        if message.sender != self.target or message.target != self.sender:
            raise ValidationError("FIX message sender and target do not match the session")
        expected = self.store.next_inbound
        if message.sequence_number > expected:
            self.events.append(SessionEvent("sequence_gap", timestamp, message.sequence_number, message.message_type, "expected %d" % expected))
            raise StorageConflictError("FIX inbound sequence has a gap")
        if message.sequence_number < expected:
            raise StorageConflictError("FIX inbound sequence is lower than expected")
        self.store.save(StoredFixMessage("inbound", message.sequence_number, message, raw, timestamp))
        self.last_received = timestamp
        self.events.append(SessionEvent("received", timestamp, message.sequence_number, message.message_type))
        self._handle_admin(message, timestamp)
        return message

    def _handle_admin(self, message: FixMessage, at: datetime) -> None:
        if message.message_type == "A":
            if self.state not in {SessionState.LOGON_SENT, SessionState.DISCONNECTED}:
                raise ValidationError("unexpected FIX logon")
            self.state = SessionState.ACTIVE
        elif message.message_type == "5":
            self.state = SessionState.TERMINATED
        elif message.message_type == "0":
            test_id = message.get(112)
            if test_id and test_id == self.test_request_id:
                self.test_request_id = None
        elif message.message_type == "1":
            test_id = message.get(112)
            if test_id:
                self._send(self._message("0", at, ((112, test_id),)), at)

    def heartbeat_due(self, now: datetime) -> bool:
        timestamp = ensure_utc(now, "FIX heartbeat check")
        return self.state is SessionState.ACTIVE and (
            self.last_sent is None or timestamp - self.last_sent >= timedelta(seconds=self.heartbeat_interval)
        )

    def heartbeat(self, at: datetime) -> bytes:
        if self.state is not SessionState.ACTIVE:
            raise ValidationError("FIX session is not active")
        timestamp = ensure_utc(at, "FIX heartbeat time")
        return self._send(self._message("0", timestamp), timestamp)

    def peer_timed_out(self, now: datetime, multiplier: float = 1.5) -> bool:
        timestamp = ensure_utc(now, "FIX timeout check")
        return self.state is SessionState.ACTIVE and self.last_received is not None and (
            timestamp - self.last_received > timedelta(seconds=self.heartbeat_interval * multiplier)
        )

    def test_request(self, request_id: str, at: datetime) -> bytes:
        identifier = request_id.strip()
        if not identifier:
            raise ValidationError("FIX test request id cannot be blank")
        if self.test_request_id is not None:
            raise ValidationError("FIX test request is already outstanding")
        timestamp = ensure_utc(at, "FIX test request time")
        raw = self._send(self._message("1", timestamp, ((112, identifier),)), timestamp)
        self.test_request_id = identifier
        return raw
