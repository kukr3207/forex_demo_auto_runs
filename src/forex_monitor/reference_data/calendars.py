"""Business-day arithmetic across one or more settlement centres."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from threading import RLock
from typing import Dict, Iterable, Optional, Tuple
from zoneinfo import ZoneInfo

from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError
from forex_monitor.reference_data.models import DayRoll, HolidayCalendar, MarketSession, required


class BusinessCalendarService:
    def __init__(self) -> None:
        self._calendars: Dict[str, HolidayCalendar] = {}
        self._sessions: Dict[str, MarketSession] = {}
        self._lock = RLock()

    def add_calendar(self, calendar: HolidayCalendar) -> HolidayCalendar:
        if not isinstance(calendar, HolidayCalendar):
            raise ValidationError("calendar must be HolidayCalendar")
        with self._lock:
            existing = self._calendars.get(calendar.calendar_id)
            if existing is not None:
                if existing == calendar:
                    return existing
                raise StorageConflictError("calendar id already exists")
            self._calendars[calendar.calendar_id] = calendar
            return calendar

    def add_session(self, session: MarketSession) -> MarketSession:
        if not isinstance(session, MarketSession):
            raise ValidationError("session must be MarketSession")
        with self._lock:
            existing = self._sessions.get(session.name)
            if existing is not None and existing != session:
                raise StorageConflictError("market session already exists")
            self._sessions[session.name] = session
            return session

    def is_business_day(self, value: date, calendar_ids: Iterable[str]) -> bool:
        calendars = self._resolve(calendar_ids, value)
        for calendar in calendars:
            if value.weekday() in calendar.weekend_days:
                return False
            if any(item.day == value and item.early_close is None for item in calendar.holidays):
                return False
        return True

    def adjust(self, value: date, calendar_ids: Iterable[str], convention: DayRoll) -> date:
        roll = DayRoll(convention)
        ids = tuple(calendar_ids)
        if roll == DayRoll.UNADJUSTED or self.is_business_day(value, ids):
            return value
        direction = 1 if roll in {DayRoll.FOLLOWING, DayRoll.MODIFIED_FOLLOWING} else -1
        changed = self._seek(value, ids, direction)
        if roll == DayRoll.MODIFIED_FOLLOWING and changed.month != value.month:
            return self._seek(value, ids, -1)
        if roll == DayRoll.MODIFIED_PRECEDING and changed.month != value.month:
            return self._seek(value, ids, 1)
        return changed

    def add_business_days(self, value: date, days: int, calendar_ids: Iterable[str]) -> date:
        ids = tuple(calendar_ids)
        if days == 0:
            return self.adjust(value, ids, DayRoll.FOLLOWING)
        direction = 1 if days > 0 else -1
        remaining = abs(days)
        current = value
        while remaining:
            current += timedelta(days=direction)
            if self.is_business_day(current, ids):
                remaining -= 1
        return current

    def business_days_between(self, start: date, end: date, calendar_ids: Iterable[str]) -> int:
        if end < start:
            return -self.business_days_between(end, start, calendar_ids)
        ids = tuple(calendar_ids)
        current = start
        count = 0
        while current < end:
            current += timedelta(days=1)
            if self.is_business_day(current, ids):
                count += 1
        return count

    def early_close(self, value: date, calendar_ids: Iterable[str]) -> Optional[time]:
        closes = []
        for calendar in self._resolve(calendar_ids, value):
            closes.extend(item.early_close for item in calendar.holidays if item.day == value and item.early_close is not None)
        return min(closes) if closes else None

    def is_session_open(self, name: str, at: datetime) -> bool:
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValidationError("session time must be timezone-aware")
        with self._lock:
            session = self._sessions.get(required(name, "session name"))
            if session is None:
                raise NotFoundError("market session does not exist")
        local = at.astimezone(ZoneInfo(session.timezone_name))
        if local.weekday() not in session.weekdays:
            return False
        current = local.timetz().replace(tzinfo=None)
        if session.opens_at < session.closes_at:
            return session.opens_at <= current < session.closes_at
        return current >= session.opens_at or current < session.closes_at

    def next_session_open(self, name: str, after: datetime) -> datetime:
        if after.tzinfo is None:
            raise ValidationError("session time must be timezone-aware")
        with self._lock:
            session = self._sessions.get(required(name, "session name"))
            if session is None:
                raise NotFoundError("market session does not exist")
        zone = ZoneInfo(session.timezone_name)
        local = after.astimezone(zone)
        for offset in range(0, 9):
            day = local.date() + timedelta(days=offset)
            if day.weekday() not in session.weekdays:
                continue
            candidate = datetime.combine(day, session.opens_at, zone)
            if candidate > local:
                return candidate.astimezone(after.tzinfo)
        raise ValidationError("no session open found within search window")

    def calendar(self, calendar_id: str) -> HolidayCalendar:
        with self._lock:
            value = self._calendars.get(required(calendar_id, "calendar id").upper())
            if value is None:
                raise NotFoundError("calendar does not exist")
            return value

    def _resolve(self, calendar_ids: Iterable[str], on: date) -> Tuple[HolidayCalendar, ...]:
        ids = tuple(calendar_ids)
        if not ids:
            raise ValidationError("at least one calendar is required")
        result = []
        with self._lock:
            for calendar_id in ids:
                calendar = self._calendars.get(calendar_id.upper())
                if calendar is None:
                    raise NotFoundError("calendar does not exist")
                if on < calendar.valid_from or (calendar.valid_until and on > calendar.valid_until):
                    raise ValidationError("calendar is not valid on requested date")
                result.append(calendar)
        return tuple(result)

    def _seek(self, value: date, calendar_ids: Tuple[str, ...], direction: int) -> date:
        current = value
        for _ in range(370):
            current += timedelta(days=direction)
            if self.is_business_day(current, calendar_ids):
                return current
        raise ValidationError("business-day adjustment exceeded search limit")
