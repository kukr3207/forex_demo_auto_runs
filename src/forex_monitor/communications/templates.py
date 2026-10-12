"""Locale-aware message templates with strict variable substitution."""

from __future__ import annotations

from threading import RLock
from typing import Dict, Mapping, Optional, Tuple

from forex_monitor.communications.models import Channel, MessageTemplate, RenderedMessage
from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError


class TemplateRegistry:
    def __init__(self, *, default_locale: str = "en") -> None:
        self.default_locale = default_locale
        self._templates: Dict[str, MessageTemplate] = {}
        self._index: Dict[Tuple[str, Channel, str], list[str]] = {}
        self._lock = RLock()

    def add(self, template: MessageTemplate) -> MessageTemplate:
        with self._lock:
            existing = self._templates.get(template.template_id)
            if existing is not None:
                if existing == template:
                    return existing
                raise StorageConflictError("message template id already exists")
            key = (template.topic, template.channel, template.locale)
            prior = [self._templates[item] for item in self._index.get(key, ())]
            if prior and template.version != max(item.version for item in prior) + 1:
                raise ValidationError("message template versions must be consecutive")
            if not prior and template.version != 1:
                raise ValidationError("first message template version must be one")
            self._templates[template.template_id] = template
            self._index.setdefault(key, []).append(template.template_id)
            return template

    def resolve(self, topic: str, channel: Channel, locale: str) -> MessageTemplate:
        with self._lock:
            for candidate in (locale, locale.split("-", 1)[0], self.default_locale):
                values = [self._templates[item] for item in self._index.get((topic.lower(), Channel(channel), candidate), ()) if self._templates[item].active]
                if values:
                    return max(values, key=lambda item: item.version)
        raise NotFoundError("active message template does not exist")

    def render(self, topic: str, channel: Channel, locale: str, variables: Mapping[str, object]) -> RenderedMessage:
        template = self.resolve(topic, channel, locale)
        missing = set(template.required_variables) - set(variables)
        if missing:
            raise ValidationError("message variables are missing: %s" % sorted(missing)[0])
        values = {key: str(value) for key, value in variables.items()}
        try:
            subject = template.subject_template.format_map(values) if template.subject_template else None
            body = template.body_template.format_map(values)
        except KeyError as exc:
            raise ValidationError("message template references undeclared variable") from exc
        return RenderedMessage(template.template_id, template.version, subject, body, tuple(sorted(values.items())))
