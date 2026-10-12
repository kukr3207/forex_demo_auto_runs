"""Campaign scheduling and deterministic audience fan-out."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from threading import RLock
from typing import Dict, Optional, Tuple

from forex_monitor.communications.delivery import MessageDeliveryService
from forex_monitor.communications.models import Campaign, CampaignState, Channel, MessageState, text, utc
from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError


class CampaignService:
    def __init__(self, delivery: MessageDeliveryService) -> None:
        self.delivery = delivery
        self._campaigns: Dict[str, Campaign] = {}
        self._delivery_ids: Dict[str, list[str]] = {}
        self._lock = RLock()

    def create(self, campaign: Campaign) -> Campaign:
        with self._lock:
            existing = self._campaigns.get(campaign.campaign_id)
            if existing is not None:
                if existing == campaign:
                    return existing
                raise StorageConflictError("campaign id already exists")
            self._campaigns[campaign.campaign_id] = campaign
            self._delivery_ids[campaign.campaign_id] = []
            return campaign

    def schedule(self, campaign_id: str, *, expected_version: int) -> Campaign:
        with self._lock:
            current = self._require(campaign_id)
            if current.version != expected_version:
                raise StorageConflictError("campaign version does not match")
            if current.state != CampaignState.DRAFT:
                raise ValidationError("only draft campaigns can be scheduled")
            changed = replace(current, state=CampaignState.SCHEDULED, version=current.version + 1)
            self._campaigns[current.campaign_id] = changed
            return changed

    def dispatch(self, campaign_id: str, *, at: datetime, channel: Optional[Channel] = None) -> Campaign:
        instant = utc(at, "campaign time")
        with self._lock:
            current = self._require(campaign_id)
            if current.state == CampaignState.COMPLETED:
                return current
            if current.state != CampaignState.SCHEDULED or current.scheduled_at > instant:
                raise ValidationError("campaign is not ready")
            variables = dict(current.variables)
            for client_id in current.audience:
                delivery_id = "%s:%s" % (current.campaign_id, client_id)
                delivery = self.delivery.create(delivery_id=delivery_id, client_id=client_id, topic=current.topic, variables={**variables, "client_id": client_id}, created_at=instant, channel=channel, campaign_id=current.campaign_id)
                if delivery_id not in self._delivery_ids[current.campaign_id]:
                    self._delivery_ids[current.campaign_id].append(delivery_id)
            changed = replace(current, state=CampaignState.RUNNING, version=current.version + 1)
            self._campaigns[current.campaign_id] = changed
            return changed

    def refresh(self, campaign_id: str) -> Campaign:
        with self._lock:
            current = self._require(campaign_id)
            if current.state != CampaignState.RUNNING:
                return current
            deliveries = self.delivery.deliveries(campaign_id=campaign_id)
            if deliveries and all(item.state in {MessageState.DELIVERED, MessageState.DEAD, MessageState.SUPPRESSED, MessageState.CANCELLED} for item in deliveries):
                current = replace(current, state=CampaignState.COMPLETED, version=current.version + 1)
                self._campaigns[current.campaign_id] = current
            return current

    def statistics(self, campaign_id: str) -> Dict[str, int]:
        self._require(campaign_id)
        result = {state.value: 0 for state in MessageState}
        for delivery in self.delivery.deliveries(campaign_id=campaign_id):
            result[delivery.state.value] += 1
        return result

    def _require(self, campaign_id: str) -> Campaign:
        value = self._campaigns.get(text(campaign_id, "campaign id"))
        if value is None:
            raise NotFoundError("campaign does not exist")
        return value
