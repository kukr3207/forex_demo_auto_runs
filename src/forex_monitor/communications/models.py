"""Communication preferences, templates, campaigns, and delivery records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from enum import Enum
from typing import Mapping, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from forex_monitor.errors import ValidationError


def text(value: object, name: str, maximum: Optional[int] = None) -> str:
    result = str(value).strip() if value is not None else ""
    if not result:
        raise ValidationError("%s is required" % name)
    if maximum is not None and len(result) > maximum:
        raise ValidationError("%s exceeds maximum length" % name)
    return result


def utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError("%s must be timezone-aware" % name)
    return value.astimezone(timezone.utc)


class Channel(str, Enum):
    EMAIL = "email"
    SMS = "sms"
    PUSH = "push"
    WEBHOOK = "webhook"
    IN_APP = "in_app"


class MessageState(str, Enum):
    PENDING = "pending"
    SUPPRESSED = "suppressed"
    SCHEDULED = "scheduled"
    DELIVERING = "delivering"
    DELIVERED = "delivered"
    FAILED = "failed"
    DEAD = "dead"
    CANCELLED = "cancelled"


class CampaignState(str, Enum):
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class ContactPoint:
    contact_id: str
    client_id: str
    channel: Channel
    address: str
    verified_at: Optional[datetime] = None
    primary: bool = False
    active: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "contact_id", text(self.contact_id, "contact id"))
        object.__setattr__(self, "client_id", text(self.client_id, "client id"))
        object.__setattr__(self, "channel", Channel(self.channel))
        address = text(self.address, "contact address", 1000)
        if self.channel == Channel.EMAIL and ("@" not in address or address.startswith("@") or address.endswith("@")):
            raise ValidationError("email address is invalid")
        if self.channel == Channel.SMS and (not address.startswith("+") or not address[1:].isdigit()):
            raise ValidationError("SMS address must use E.164 format")
        if self.channel == Channel.WEBHOOK and not address.startswith("https://"):
            raise ValidationError("webhook address must use HTTPS")
        object.__setattr__(self, "address", address)
        if self.verified_at is not None:
            object.__setattr__(self, "verified_at", utc(self.verified_at, "verified at"))


@dataclass(frozen=True)
class CommunicationPreference:
    client_id: str
    topic: str
    enabled_channels: Tuple[Channel, ...]
    locale: str = "en"
    timezone_name: str = "UTC"
    quiet_start: Optional[time] = None
    quiet_end: Optional[time] = None
    mandatory: bool = False
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "client_id", text(self.client_id, "client id"))
        object.__setattr__(self, "topic", text(self.topic, "topic").lower())
        channels = tuple(sorted(set(Channel(item) for item in self.enabled_channels), key=lambda item: item.value))
        if not channels and self.mandatory:
            raise ValidationError("mandatory topic needs an enabled channel")
        object.__setattr__(self, "enabled_channels", channels)
        locale = text(self.locale, "locale")
        if len(locale) < 2:
            raise ValidationError("locale is invalid")
        object.__setattr__(self, "locale", locale)
        try:
            ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValidationError("preference timezone is unknown") from exc
        if (self.quiet_start is None) != (self.quiet_end is None):
            raise ValidationError("quiet hours require start and end")


@dataclass(frozen=True)
class MessageTemplate:
    template_id: str
    topic: str
    channel: Channel
    locale: str
    subject_template: Optional[str]
    body_template: str
    required_variables: Tuple[str, ...]
    created_at: datetime
    version: int = 1
    active: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "template_id", text(self.template_id, "template id"))
        object.__setattr__(self, "topic", text(self.topic, "topic").lower())
        object.__setattr__(self, "channel", Channel(self.channel))
        object.__setattr__(self, "locale", text(self.locale, "locale"))
        if self.channel == Channel.EMAIL and not self.subject_template:
            raise ValidationError("email template requires a subject")
        object.__setattr__(self, "body_template", text(self.body_template, "body template", 20000))
        variables = tuple(sorted(set(self.required_variables)))
        if any("{" + item + "}" not in self.body_template and (not self.subject_template or "{" + item + "}" not in self.subject_template) for item in variables):
            raise ValidationError("required template variable is not referenced")
        object.__setattr__(self, "required_variables", variables)
        object.__setattr__(self, "created_at", utc(self.created_at, "created at"))


@dataclass(frozen=True)
class RenderedMessage:
    template_id: str
    template_version: int
    subject: Optional[str]
    body: str
    variables: Tuple[Tuple[str, str], ...]


@dataclass(frozen=True)
class Campaign:
    campaign_id: str
    name: str
    topic: str
    audience: Tuple[str, ...]
    variables: Tuple[Tuple[str, str], ...]
    scheduled_at: datetime
    created_at: datetime
    state: CampaignState = CampaignState.DRAFT
    priority: int = 5
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "campaign_id", text(self.campaign_id, "campaign id"))
        object.__setattr__(self, "name", text(self.name, "campaign name"))
        object.__setattr__(self, "topic", text(self.topic, "topic").lower())
        audience = tuple(sorted(set(item.strip() for item in self.audience if item.strip())))
        if not audience:
            raise ValidationError("campaign audience is empty")
        object.__setattr__(self, "audience", audience)
        object.__setattr__(self, "scheduled_at", utc(self.scheduled_at, "scheduled at"))
        object.__setattr__(self, "created_at", utc(self.created_at, "created at"))
        object.__setattr__(self, "state", CampaignState(self.state))
        if self.priority < 1 or self.priority > 10:
            raise ValidationError("campaign priority is invalid")


@dataclass(frozen=True)
class MessageDelivery:
    delivery_id: str
    client_id: str
    topic: str
    channel: Channel
    contact_id: str
    message: RenderedMessage
    state: MessageState
    created_at: datetime
    scheduled_at: datetime
    campaign_id: Optional[str] = None
    attempt_count: int = 0
    provider_reference: Optional[str] = None
    response: Optional[str] = None
    completed_at: Optional[datetime] = None
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "delivery_id", text(self.delivery_id, "delivery id"))
        object.__setattr__(self, "client_id", text(self.client_id, "client id"))
        object.__setattr__(self, "topic", text(self.topic, "topic").lower())
        object.__setattr__(self, "channel", Channel(self.channel))
        object.__setattr__(self, "contact_id", text(self.contact_id, "contact id"))
        object.__setattr__(self, "state", MessageState(self.state))
        object.__setattr__(self, "created_at", utc(self.created_at, "created at"))
        object.__setattr__(self, "scheduled_at", utc(self.scheduled_at, "scheduled at"))
