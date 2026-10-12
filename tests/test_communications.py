from datetime import datetime, time, timedelta, timezone

import pytest

from forex_monitor.communications.campaigns import CampaignService
from forex_monitor.communications.delivery import MessageDeliveryService
from forex_monitor.communications.models import Campaign, CampaignState, Channel, CommunicationPreference, ContactPoint, MessageState, MessageTemplate
from forex_monitor.communications.preferences import PreferenceService
from forex_monitor.communications.templates import TemplateRegistry
from forex_monitor.errors import ValidationError


NOW = datetime(2026, 10, 12, 8, tzinfo=timezone.utc)


def platform(clients=("client",)):
    preferences = PreferenceService()
    for client in clients:
        preferences.add_contact(ContactPoint("%s-email" % client, client, Channel.EMAIL, "%s@example.test" % client, verified_at=NOW, primary=True))
        preferences.set_preference(CommunicationPreference(client, "trade", (Channel.EMAIL,), locale="en"))
    templates = TemplateRegistry()
    templates.add(MessageTemplate("trade-en", "trade", Channel.EMAIL, "en", "Trade {trade_id}", "Hello {client_id}, trade {trade_id} is ready", ("client_id", "trade_id"), NOW))
    delivery = MessageDeliveryService(preferences, templates)
    return preferences, templates, delivery


def test_template_fallback_and_variable_validation() -> None:
    _, templates, _ = platform()
    rendered = templates.render("trade", Channel.EMAIL, "en-GB", {"client_id": "client", "trade_id": "T1"})
    assert rendered.subject == "Trade T1"
    with pytest.raises(ValidationError):
        templates.render("trade", Channel.EMAIL, "en", {"client_id": "client"})


def test_preference_blocks_disabled_channel() -> None:
    _, _, delivery = platform()
    with pytest.raises(ValidationError):
        delivery.create(delivery_id="one", client_id="client", topic="trade", variables={"client_id": "client", "trade_id": "T1"}, created_at=NOW, channel=Channel.SMS)


def test_delivery_retries_then_succeeds() -> None:
    _, _, delivery = platform()
    message = delivery.create(delivery_id="one", client_id="client", topic="trade", variables={"client_id": "client", "trade_id": "T1"}, created_at=NOW)
    claimed = delivery.claim(message.delivery_id, at=NOW, expected_version=1)
    failed = delivery.record_result(message.delivery_id, delivered=False, provider_reference="", response="timeout", at=NOW, expected_version=claimed.version)
    assert failed.state == MessageState.FAILED
    claimed = delivery.claim(message.delivery_id, at=failed.scheduled_at, expected_version=failed.version)
    sent = delivery.record_result(message.delivery_id, delivered=True, provider_reference="provider-1", response="ok", at=failed.scheduled_at, expected_version=claimed.version)
    assert sent.state == MessageState.DELIVERED


def test_quiet_hours_schedule_message_for_later() -> None:
    preferences, templates, _ = platform()
    preferences.set_preference(CommunicationPreference("client", "trade", (Channel.EMAIL,), timezone_name="UTC", quiet_start=time(7), quiet_end=time(9)), expected_version=1)
    delivery = MessageDeliveryService(preferences, templates)
    message = delivery.create(delivery_id="quiet", client_id="client", topic="trade", variables={"client_id": "client", "trade_id": "T1"}, created_at=NOW)
    assert message.state == MessageState.SCHEDULED
    assert message.scheduled_at.hour == 9


def test_campaign_fans_out_and_completes() -> None:
    _, _, delivery = platform(("a", "b"))
    campaigns = CampaignService(delivery)
    campaign = campaigns.create(Campaign("campaign", "Trade notice", "trade", ("a", "b"), (("trade_id", "T1"),), NOW, NOW))
    campaigns.schedule(campaign.campaign_id, expected_version=1)
    running = campaigns.dispatch(campaign.campaign_id, at=NOW)
    assert running.state == CampaignState.RUNNING
    for message in delivery.deliveries(campaign_id=campaign.campaign_id):
        claimed = delivery.claim(message.delivery_id, at=NOW, expected_version=message.version)
        delivery.record_result(message.delivery_id, delivered=True, provider_reference="p", response="ok", at=NOW, expected_version=claimed.version)
    assert campaigns.refresh(campaign.campaign_id).state == CampaignState.COMPLETED
    assert campaigns.statistics(campaign.campaign_id)["delivered"] == 2
