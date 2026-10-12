"""FIX field and message dictionaries with type-aware validation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Dict, Iterable, Mapping, Optional, Sequence, Tuple

from forex_monitor.errors import ValidationError


class FixDataType(str, Enum):
    STRING = "STRING"
    CHAR = "CHAR"
    INT = "INT"
    LENGTH = "LENGTH"
    NUMINGROUP = "NUMINGROUP"
    SEQNUM = "SEQNUM"
    FLOAT = "FLOAT"
    PRICE = "PRICE"
    QTY = "QTY"
    AMT = "AMT"
    PERCENTAGE = "PERCENTAGE"
    BOOLEAN = "BOOLEAN"
    UTCTIMESTAMP = "UTCTIMESTAMP"
    UTCDATEONLY = "UTCDATEONLY"
    UTCTIMEONLY = "UTCTIMEONLY"


@dataclass(frozen=True)
class FixField:
    tag: int
    name: str
    data_type: FixDataType = FixDataType.STRING
    enums: Mapping[str, str] = None

    def __post_init__(self) -> None:
        if self.tag <= 0:
            raise ValidationError("FIX field tag must be positive")
        if not self.name.strip():
            raise ValidationError("FIX field name cannot be blank")
        object.__setattr__(self, "data_type", self.data_type if isinstance(self.data_type, FixDataType) else FixDataType(self.data_type))
        object.__setattr__(self, "enums", dict(self.enums or {}))

    def validate(self, value: str) -> None:
        if "\x01" in value or "=" in value:
            raise ValidationError("FIX field value contains a reserved delimiter")
        if self.enums and value not in self.enums:
            raise ValidationError("FIX field value is outside its enumeration")
        try:
            if self.data_type in {FixDataType.INT, FixDataType.LENGTH, FixDataType.NUMINGROUP, FixDataType.SEQNUM}:
                parsed = int(value)
                if parsed < 0 or (self.data_type is FixDataType.SEQNUM and parsed == 0):
                    raise ValueError
            elif self.data_type in {FixDataType.FLOAT, FixDataType.PRICE, FixDataType.QTY, FixDataType.AMT, FixDataType.PERCENTAGE}:
                if not Decimal(value).is_finite():
                    raise ValueError
            elif self.data_type is FixDataType.CHAR and len(value) != 1:
                raise ValueError
            elif self.data_type is FixDataType.BOOLEAN and value not in {"Y", "N"}:
                raise ValueError
            elif self.data_type is FixDataType.UTCTIMESTAMP:
                datetime.strptime(value, "%Y%m%d-%H:%M:%S.%f" if "." in value else "%Y%m%d-%H:%M:%S")
            elif self.data_type is FixDataType.UTCDATEONLY:
                datetime.strptime(value, "%Y%m%d")
            elif self.data_type is FixDataType.UTCTIMEONLY:
                datetime.strptime(value, "%H:%M:%S.%f" if "." in value else "%H:%M:%S")
        except (ValueError, InvalidOperation) as exc:
            raise ValidationError("invalid value for FIX field %s" % self.name) from exc


@dataclass(frozen=True)
class FixMessageType:
    code: str
    name: str
    required_tags: Tuple[int, ...] = ()
    allowed_tags: Tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if not self.code or not self.name.strip():
            raise ValidationError("FIX message code and name cannot be blank")
        if len(set(self.required_tags)) != len(self.required_tags):
            raise ValidationError("FIX message required tags must be unique")
        if self.allowed_tags and not set(self.required_tags).issubset(self.allowed_tags):
            raise ValidationError("FIX required tags must also be allowed")


class FixDictionary:
    def __init__(self, begin_string: str = "FIX.4.4") -> None:
        self.begin_string = begin_string.strip()
        self._fields: Dict[int, FixField] = {}
        self._field_names: Dict[str, int] = {}
        self._messages: Dict[str, FixMessageType] = {}

    def add_field(self, field: FixField) -> None:
        if field.tag in self._fields or field.name in self._field_names:
            raise ValidationError("FIX field tag or name is already registered")
        self._fields[field.tag] = field
        self._field_names[field.name] = field.tag

    def add_message(self, message: FixMessageType) -> None:
        if message.code in self._messages:
            raise ValidationError("FIX message type is already registered")
        unknown = [tag for tag in message.required_tags + message.allowed_tags if tag not in self._fields]
        if unknown:
            raise ValidationError("FIX message references unknown fields")
        self._messages[message.code] = message

    def field(self, tag_or_name: object) -> FixField:
        if isinstance(tag_or_name, str) and not tag_or_name.isdigit():
            tag = self._field_names.get(tag_or_name)
        else:
            try:
                tag = int(tag_or_name)
            except (TypeError, ValueError):
                tag = None
        if tag is None or tag not in self._fields:
            raise ValidationError("FIX field is not registered")
        return self._fields[tag]

    def message(self, code: str) -> FixMessageType:
        try:
            return self._messages[code]
        except KeyError as exc:
            raise ValidationError("FIX message type is not registered") from exc

    def validate(self, message_type: str, fields: Mapping[int, str]) -> Tuple[str, ...]:
        definition = self.message(message_type)
        errors = []
        for tag in definition.required_tags:
            if tag not in fields:
                errors.append("required field %d is missing" % tag)
        allowed = set(definition.allowed_tags)
        for tag, value in fields.items():
            try:
                field = self.field(tag)
            except ValidationError:
                errors.append("field %d is unknown" % tag)
                continue
            if allowed and tag not in allowed:
                errors.append("field %d is not allowed for message %s" % (tag, message_type))
                continue
            try:
                field.validate(value)
            except ValidationError as error:
                errors.append(str(error))
        return tuple(errors)

    @classmethod
    def forex(cls) -> "FixDictionary":
        dictionary = cls("FIX.4.4")
        fields = (
            FixField(8, "BeginString"),
            FixField(9, "BodyLength", FixDataType.LENGTH),
            FixField(10, "CheckSum"),
            FixField(11, "ClOrdID"),
            FixField(14, "CumQty", FixDataType.QTY),
            FixField(15, "Currency"),
            FixField(17, "ExecID"),
            FixField(21, "HandlInst", FixDataType.CHAR),
            FixField(31, "LastPx", FixDataType.PRICE),
            FixField(32, "LastQty", FixDataType.QTY),
            FixField(34, "MsgSeqNum", FixDataType.SEQNUM),
            FixField(35, "MsgType"),
            FixField(37, "OrderID"),
            FixField(38, "OrderQty", FixDataType.QTY),
            FixField(39, "OrdStatus", FixDataType.CHAR),
            FixField(40, "OrdType", FixDataType.CHAR),
            FixField(41, "OrigClOrdID"),
            FixField(44, "Price", FixDataType.PRICE),
            FixField(49, "SenderCompID"),
            FixField(52, "SendingTime", FixDataType.UTCTIMESTAMP),
            FixField(54, "Side", FixDataType.CHAR, {"1": "Buy", "2": "Sell"}),
            FixField(55, "Symbol"),
            FixField(56, "TargetCompID"),
            FixField(58, "Text"),
            FixField(59, "TimeInForce", FixDataType.CHAR),
            FixField(60, "TransactTime", FixDataType.UTCTIMESTAMP),
            FixField(98, "EncryptMethod", FixDataType.INT),
            FixField(108, "HeartBtInt", FixDataType.INT),
            FixField(112, "TestReqID"),
            FixField(141, "ResetSeqNumFlag", FixDataType.BOOLEAN),
            FixField(150, "ExecType", FixDataType.CHAR),
            FixField(151, "LeavesQty", FixDataType.QTY),
            FixField(553, "Username"),
            FixField(554, "Password"),
        )
        for field in fields:
            dictionary.add_field(field)
        header = (8, 9, 35, 49, 56, 34, 52, 10)
        dictionary.add_message(FixMessageType("0", "Heartbeat", (), header + (112,)))
        dictionary.add_message(FixMessageType("1", "TestRequest", (112,), header + (112,)))
        dictionary.add_message(FixMessageType("2", "ResendRequest", (), header + (7, 16))) if False else None
        dictionary.add_message(FixMessageType("5", "Logout", (), header + (58,)))
        dictionary.add_message(FixMessageType("A", "Logon", (98, 108), header + (98, 108, 141, 553, 554)))
        order_fields = header + (11, 21, 38, 40, 44, 54, 55, 59, 60, 15)
        dictionary.add_message(FixMessageType("D", "NewOrderSingle", (11, 21, 38, 40, 54, 55, 60), order_fields))
        cancel_fields = header + (11, 37, 41, 54, 55, 60, 38)
        dictionary.add_message(FixMessageType("F", "OrderCancelRequest", (11, 41, 54, 55, 60), cancel_fields))
        execution_fields = header + (11, 14, 17, 31, 32, 37, 38, 39, 54, 55, 150, 151, 58)
        dictionary.add_message(FixMessageType("8", "ExecutionReport", (17, 37, 39, 54, 55, 150), execution_fields))
        return dictionary
