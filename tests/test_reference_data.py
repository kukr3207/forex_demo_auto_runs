from datetime import date, datetime, time, timezone
from decimal import Decimal

import pytest

from forex_monitor.errors import StorageConflictError, ValidationError
from forex_monitor.reference_data.calendars import BusinessCalendarService
from forex_monitor.reference_data.identifiers import IdentifierRegistry
from forex_monitor.reference_data.instruments import InstrumentRegistry
from forex_monitor.reference_data.models import CurrencyDefinition, DayRoll, FxInstrument, Holiday, HolidayCalendar, IdentifierAssignment, IdentifierType, InstrumentType, MarketSession
from forex_monitor.reference_data.service import ReferenceDataService


DAY = date(2026, 10, 12)


def calendars():
    service = BusinessCalendarService()
    service.add_calendar(HolidayCalendar("NYC", "New York", "America/New_York", (Holiday(date(2026, 10, 14), "Holiday"),)))
    service.add_calendar(HolidayCalendar("TARGET", "TARGET", "Europe/Paris", ()))
    return service


def instruments():
    registry = InstrumentRegistry()
    registry.add_currency(CurrencyDefinition("EUR", "978", "Euro", 2, settlement_calendar="TARGET"))
    registry.add_currency(CurrencyDefinition("USD", "840", "US Dollar", 2, settlement_calendar="NYC"))
    registry.add_currency(CurrencyDefinition("INR", "356", "Indian Rupee", 2, deliverable=False, restricted=True))
    registry.add_instrument(FxInstrument("eurusd-spot", "EURUSD", InstrumentType.SPOT, "EUR", "USD", 5, 0, Decimal("1000"), Decimal("1000"), 2, ("NYC", "TARGET")))
    return registry


def test_joint_calendar_skips_weekends_and_holidays() -> None:
    service = calendars()
    assert service.add_business_days(DAY, 2, ("NYC", "TARGET")) == date(2026, 10, 15)
    assert service.business_days_between(DAY, date(2026, 10, 16), ("NYC",)) == 3


def test_modified_following_stays_in_original_month() -> None:
    service = calendars()
    assert service.adjust(date(2026, 10, 31), ("NYC",), DayRoll.MODIFIED_FOLLOWING) == date(2026, 10, 30)


def test_market_session_handles_local_timezone() -> None:
    service = calendars()
    service.add_session(MarketSession("London", "Europe/London", time(8), time(17)))
    assert service.is_session_open("London", datetime(2026, 10, 12, 12, tzinfo=timezone.utc))
    assert not service.is_session_open("London", datetime(2026, 10, 12, 20, tzinfo=timezone.utc))


def test_instrument_requires_registered_currencies() -> None:
    registry = InstrumentRegistry()
    with pytest.raises(Exception):
        registry.add_instrument(FxInstrument("pair", "EURUSD", InstrumentType.SPOT, "EUR", "USD", 5, 0, 1, 1, 2, ("NYC",)))


def test_currency_effective_dates_cannot_overlap() -> None:
    registry = InstrumentRegistry()
    registry.add_currency(CurrencyDefinition("USD", "840", "Dollar", 2))
    with pytest.raises(StorageConflictError):
        registry.add_currency(CurrencyDefinition("USD", "840", "New Dollar", 2, effective_from=date(2020, 1, 1)))


def test_instrument_amendment_closes_old_version() -> None:
    registry = instruments()
    changed = registry.amend_instrument("eurusd-spot", effective_from=date(2027, 1, 1), expected_version=1, minimum_quantity=Decimal("5000"), quantity_increment=Decimal("1000"))
    assert changed.version == 2
    assert registry.instrument("eurusd-spot", on=date(2026, 12, 31)).minimum_quantity == Decimal("1000")
    assert registry.instrument("eurusd-spot", on=date(2027, 1, 1)).minimum_quantity == Decimal("5000")


def test_identifier_registry_resolves_temporal_assignment() -> None:
    registry = IdentifierRegistry()
    assignment = IdentifierAssignment(IdentifierType.RIC, "EUR=", "instrument", "eurusd-spot", DAY, primary=True)
    registry.assign(assignment)
    assert registry.resolve(IdentifierType.RIC, "eur=", on=DAY) == assignment
    assert registry.primary("instrument", "eurusd-spot", IdentifierType.RIC, on=DAY) == assignment


def test_identifier_collision_is_rejected() -> None:
    registry = IdentifierRegistry()
    registry.assign(IdentifierAssignment(IdentifierType.RIC, "EUR=", "instrument", "one", DAY))
    with pytest.raises(StorageConflictError):
        registry.assign(IdentifierAssignment(IdentifierType.RIC, "EUR=", "instrument", "two", DAY))


def test_reference_service_resolves_settlement_and_normalizes_order() -> None:
    service = ReferenceDataService(instruments(), calendars())
    terms = service.settlement_terms("eurusd-spot", trade_date=DAY)
    assert terms.spot_date == date(2026, 10, 15)
    order = service.normalize_order("eurusd-spot", quantity="10000", limit_price="1.123456", trade_date=DAY)
    assert order.limit_price == Decimal("1.12346")
    assert order.value_date == terms.spot_date


def test_order_quantity_must_follow_increment() -> None:
    service = ReferenceDataService(instruments(), calendars())
    with pytest.raises(ValidationError):
        service.normalize_order("eurusd-spot", quantity="10500", limit_price=None, trade_date=DAY)


def test_ndf_has_fixing_before_value_date() -> None:
    registry = instruments()
    registry.add_instrument(FxInstrument("usdinr-ndf", "USDINR", InstrumentType.NDF, "USD", "INR", 4, 0, 1000, 1000, 2, ("NYC",), fixing_source="RBIB", settlement_currency="USD"))
    service = ReferenceDataService(registry, calendars())
    terms = service.settlement_terms("usdinr-ndf", trade_date=DAY, tenor_days=30)
    assert terms.fixing_date < terms.value_date
    assert terms.settlement_currency == "USD"
