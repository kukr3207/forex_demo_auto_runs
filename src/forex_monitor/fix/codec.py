"""FIX tag-value encoding, framing, checksum, and stream decoding."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Mapping, Optional, Sequence, Tuple

from forex_monitor.errors import ValidationError
from forex_monitor.fix.dictionary import FixDictionary


SOH = b"\x01"


@dataclass(frozen=True)
class FixMessage:
    message_type: str
    sequence_number: int
    sender: str
    target: str
    sending_time: str
    fields: Tuple[Tuple[int, str], ...] = ()
    begin_string: str = "FIX.4.4"

    def __post_init__(self) -> None:
        if self.sequence_number <= 0:
            raise ValidationError("FIX sequence number must be positive")
        if not all((self.message_type, self.sender.strip(), self.target.strip(), self.sending_time.strip())):
            raise ValidationError("FIX message header fields cannot be blank")
        tags = [tag for tag, _ in self.fields]
        if len(tags) != len(set(tags)):
            raise ValidationError("FIX message fields cannot repeat tags")
        if any(tag in {8, 9, 10, 34, 35, 49, 52, 56} for tag in tags):
            raise ValidationError("FIX body cannot override framing or session header fields")

    def get(self, tag: int, default: Optional[str] = None) -> Optional[str]:
        for candidate, value in self.fields:
            if candidate == tag:
                return value
        return default

    def as_mapping(self) -> Mapping[int, str]:
        return dict(self.fields)


class FixCodec:
    def __init__(self, dictionary: Optional[FixDictionary] = None) -> None:
        self.dictionary = dictionary or FixDictionary.forex()
        self._buffer = b""

    @staticmethod
    def checksum(payload: bytes) -> int:
        return sum(payload) % 256

    def encode(self, message: FixMessage) -> bytes:
        body_fields = (
            (35, message.message_type),
            (49, message.sender),
            (56, message.target),
            (34, str(message.sequence_number)),
            (52, message.sending_time),
        ) + message.fields
        validation_fields = dict(body_fields)
        errors = self.dictionary.validate(message.message_type, validation_fields)
        if errors:
            raise ValidationError("FIX message failed dictionary validation", details={"errors": errors})
        body = SOH.join(("%d=%s" % item).encode("ascii") for item in body_fields) + SOH
        prefix = ("8=%s\x019=%d\x01" % (message.begin_string, len(body))).encode("ascii")
        framed = prefix + body
        trailer = ("10=%03d\x01" % self.checksum(framed)).encode("ascii")
        return framed + trailer

    def decode(self, payload: bytes, *, validate: bool = True) -> FixMessage:
        if not payload.endswith(SOH):
            raise ValidationError("FIX message must end with SOH")
        pairs = []
        for raw in payload[:-1].split(SOH):
            try:
                tag_text, value = raw.split(b"=", 1)
                tag = int(tag_text)
                decoded = value.decode("ascii")
            except (ValueError, UnicodeDecodeError) as exc:
                raise ValidationError("FIX message contains a malformed field") from exc
            pairs.append((tag, decoded))
        mapping = dict(pairs)
        if len(mapping) != len(pairs):
            raise ValidationError("FIX message contains repeated tags")
        for required in (8, 9, 35, 49, 56, 34, 52, 10):
            if required not in mapping:
                raise ValidationError("FIX message is missing framing field %d" % required)
        checksum_position = payload.rfind(b"10=")
        if checksum_position < 0 or int(mapping[10]) != self.checksum(payload[:checksum_position]):
            raise ValidationError("FIX message checksum does not match")
        body_start = payload.find(SOH, payload.find(SOH) + 1) + 1
        expected_length = int(mapping[9])
        if checksum_position - body_start != expected_length:
            raise ValidationError("FIX body length does not match")
        header_tags = {8, 9, 10, 34, 35, 49, 52, 56}
        body = tuple((tag, value) for tag, value in pairs if tag not in header_tags)
        message = FixMessage(mapping[35], int(mapping[34]), mapping[49], mapping[56], mapping[52], body, mapping[8])
        if validate:
            errors = self.dictionary.validate(message.message_type, dict(pairs))
            if errors:
                raise ValidationError("FIX message failed dictionary validation", details={"errors": errors})
        return message

    def feed(self, data: bytes) -> Tuple[FixMessage, ...]:
        self._buffer += data
        messages = []
        while True:
            start = self._buffer.find(b"8=FIX")
            if start < 0:
                self._buffer = self._buffer[-16:]
                break
            if start:
                self._buffer = self._buffer[start:]
            first = self._buffer.find(SOH)
            second = self._buffer.find(SOH, first + 1)
            if first < 0 or second < 0:
                break
            length_field = self._buffer[first + 1:second]
            if not length_field.startswith(b"9="):
                self._buffer = self._buffer[first + 1:]
                continue
            try:
                body_length = int(length_field[2:])
            except ValueError:
                self._buffer = self._buffer[second + 1:]
                continue
            trailer_start = second + 1 + body_length
            total_length = trailer_start + 7
            if len(self._buffer) < total_length:
                break
            candidate = self._buffer[:total_length]
            messages.append(self.decode(candidate))
            self._buffer = self._buffer[total_length:]
        return tuple(messages)
