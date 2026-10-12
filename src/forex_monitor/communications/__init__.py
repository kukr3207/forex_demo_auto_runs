"""Client message templates, preferences, campaigns, and delivery tracking."""

from forex_monitor.communications.campaigns import CampaignService
from forex_monitor.communications.delivery import MessageDeliveryService
from forex_monitor.communications.preferences import PreferenceService
from forex_monitor.communications.templates import TemplateRegistry

__all__ = ["CampaignService", "MessageDeliveryService", "PreferenceService", "TemplateRegistry"]
