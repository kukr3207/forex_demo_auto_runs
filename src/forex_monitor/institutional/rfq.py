"""Request-for-quote handling with idempotent client requests."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from threading import RLock
from typing import Dict, Iterable, Optional, Tuple

from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError
from forex_monitor.institutional.clients import ClientDirectory
from forex_monitor.institutional.models import (
    ClientStatus,
    InstitutionalTrade,
    QuoteRequest,
    RfqStatus,
    amount,
    aware,
    required,
)


class RfqService:
    def __init__(self, clients: ClientDirectory) -> None:
        self._clients = clients
        self._requests: Dict[str, QuoteRequest] = {}
        self._request_keys: Dict[str, str] = {}
        self._trades: Dict[str, InstitutionalTrade] = {}
        self._rfq_trades: Dict[str, str] = {}
        self._lock = RLock()

    def request(
        self,
        *,
        rfq_id: str,
        client_id: str,
        symbol: str,
        side: str,
        notional: object,
        notional_currency: str,
        settlement_date: date,
        requested_at: datetime,
        ttl_seconds: int = 30,
        request_key: Optional[str] = None,
    ) -> QuoteRequest:
        if ttl_seconds < 1 or ttl_seconds > 300:
            raise ValidationError("RFQ TTL must be between one and three hundred seconds")
        client = self._clients.client(client_id)
        if client is None:
            raise NotFoundError("client does not exist")
        if client.status != ClientStatus.ACTIVE:
            raise ValidationError("only active clients can request prices")
        if "spot" not in self._clients.permissions(client_id):
            raise ValidationError("client is not permitted for spot FX")
        requested = aware(requested_at, "requested at")
        key = request_key.strip() if request_key else None
        value = QuoteRequest(
            rfq_id=rfq_id,
            client_id=client_id,
            symbol=symbol,
            side=side,
            notional=Decimal(str(notional)),
            notional_currency=notional_currency,
            settlement_date=settlement_date,
            requested_at=requested,
            expires_at=requested + timedelta(seconds=ttl_seconds),
            request_key=key,
        )
        with self._lock:
            if key:
                existing_id = self._request_keys.get("%s:%s" % (client_id, key))
                if existing_id:
                    existing = self._requests[existing_id]
                    if self._request_identity(existing) != self._request_identity(value):
                        raise StorageConflictError("RFQ request key was reused with different terms")
                    return existing
            existing = self._requests.get(value.rfq_id)
            if existing is not None:
                if existing == value:
                    return existing
                raise StorageConflictError("RFQ id already exists")
            self._requests[value.rfq_id] = value
            if key:
                self._request_keys["%s:%s" % (client_id, key)] = value.rfq_id
            return value

    def quote(
        self,
        rfq_id: str,
        price: object,
        dealer_id: str,
        *,
        expected_version: int,
        quoted_at: datetime,
    ) -> QuoteRequest:
        with self._lock:
            current = self._require(rfq_id)
            self._check_version(current, expected_version)
            timestamp = aware(quoted_at, "quoted at")
            if current.status != RfqStatus.OPEN:
                raise ValidationError("only open RFQs can be quoted")
            if timestamp > current.expires_at:
                changed = replace(
                    current,
                    status=RfqStatus.EXPIRED,
                    version=current.version + 1,
                )
                self._requests[current.rfq_id] = changed
                raise ValidationError("RFQ expired before quote")
            value = replace(
                current,
                status=RfqStatus.QUOTED,
                quoted_price=amount(price, "quoted price"),
                quoted_at=timestamp,
                dealer_id=required(dealer_id, "dealer id"),
                version=current.version + 1,
            )
            self._requests[value.rfq_id] = value
            return value

    def reprice(
        self,
        rfq_id: str,
        price: object,
        *,
        expected_version: int,
        quoted_at: datetime,
        extension_seconds: int = 10,
    ) -> QuoteRequest:
        if extension_seconds < 1 or extension_seconds > 60:
            raise ValidationError("quote extension must be between one and sixty seconds")
        with self._lock:
            current = self._require(rfq_id)
            self._check_version(current, expected_version)
            timestamp = aware(quoted_at, "quoted at")
            if current.status != RfqStatus.QUOTED:
                raise ValidationError("only quoted RFQs can be repriced")
            if timestamp > current.expires_at:
                raise ValidationError("expired RFQ cannot be repriced")
            changed = replace(
                current,
                quoted_price=amount(price, "quoted price"),
                quoted_at=timestamp,
                expires_at=timestamp + timedelta(seconds=extension_seconds),
                version=current.version + 1,
            )
            self._requests[current.rfq_id] = changed
            return changed

    def accept(
        self,
        rfq_id: str,
        *,
        trade_id: str,
        accepted_at: datetime,
        expected_version: int,
        venue: str = "OTC",
        external_reference: Optional[str] = None,
    ) -> InstitutionalTrade:
        with self._lock:
            current = self._require(rfq_id)
            self._check_version(current, expected_version)
            timestamp = aware(accepted_at, "accepted at")
            existing_trade_id = self._rfq_trades.get(current.rfq_id)
            if existing_trade_id:
                existing = self._trades[existing_trade_id]
                if existing.trade_id == trade_id:
                    return existing
                raise StorageConflictError("RFQ already belongs to another trade")
            if current.status != RfqStatus.QUOTED or current.quoted_price is None:
                raise ValidationError("RFQ must have an active quote")
            if timestamp > current.expires_at:
                self._requests[current.rfq_id] = replace(
                    current,
                    status=RfqStatus.EXPIRED,
                    version=current.version + 1,
                )
                raise ValidationError("RFQ quote has expired")
            agreement = self._clients.effective_agreement(
                current.client_id,
                "spot",
                current.notional_currency,
                current.settlement_date,
            )
            if agreement is None:
                raise ValidationError("client has no effective trading agreement")
            base_amount, quote_amount = self._trade_amounts(current)
            trade = InstitutionalTrade(
                trade_id=trade_id,
                client_id=current.client_id,
                rfq_id=current.rfq_id,
                symbol=current.symbol,
                side=current.side,
                base_amount=base_amount,
                quote_amount=quote_amount,
                price=current.quoted_price,
                trade_date=timestamp.date(),
                settlement_date=current.settlement_date,
                booked_at=timestamp,
                dealer_id=current.dealer_id,
                venue=venue,
                agreement_id=agreement.agreement_id,
                external_reference=external_reference,
            )
            if trade.trade_id in self._trades:
                raise StorageConflictError("trade id already exists")
            self._trades[trade.trade_id] = trade
            self._rfq_trades[current.rfq_id] = trade.trade_id
            self._requests[current.rfq_id] = replace(
                current,
                status=RfqStatus.ACCEPTED,
                accepted_at=timestamp,
                version=current.version + 1,
            )
            return trade

    def pass_request(
        self,
        rfq_id: str,
        *,
        expected_version: int,
    ) -> QuoteRequest:
        return self._terminal(rfq_id, RfqStatus.PASSED, expected_version)

    def cancel_request(
        self,
        rfq_id: str,
        *,
        expected_version: int,
    ) -> QuoteRequest:
        return self._terminal(rfq_id, RfqStatus.CANCELLED, expected_version)

    def expire(self, at: datetime) -> Tuple[QuoteRequest, ...]:
        timestamp = aware(at, "expiry time")
        expired = []
        with self._lock:
            for rfq_id, current in list(self._requests.items()):
                if current.status in {RfqStatus.OPEN, RfqStatus.QUOTED} and current.expires_at < timestamp:
                    value = replace(
                        current,
                        status=RfqStatus.EXPIRED,
                        version=current.version + 1,
                    )
                    self._requests[rfq_id] = value
                    expired.append(value)
        return tuple(sorted(expired, key=lambda item: item.expires_at))

    def request_by_id(self, rfq_id: str) -> Optional[QuoteRequest]:
        with self._lock:
            return self._requests.get(rfq_id.strip())

    def trade(self, trade_id: str) -> Optional[InstitutionalTrade]:
        with self._lock:
            return self._trades.get(trade_id.strip())

    def list_requests(
        self,
        *,
        client_id: Optional[str] = None,
        status: Optional[RfqStatus] = None,
        dealer_id: Optional[str] = None,
        requested_from: Optional[datetime] = None,
        requested_to: Optional[datetime] = None,
    ) -> Tuple[QuoteRequest, ...]:
        status_value = RfqStatus(status) if status is not None else None
        with self._lock:
            values = tuple(
                item
                for item in self._requests.values()
                if (client_id is None or item.client_id == client_id)
                and (status_value is None or item.status == status_value)
                and (dealer_id is None or item.dealer_id == dealer_id)
                and (requested_from is None or item.requested_at >= requested_from)
                and (requested_to is None or item.requested_at < requested_to)
            )
            return tuple(sorted(values, key=lambda item: (item.requested_at, item.rfq_id)))

    def list_trades(
        self,
        *,
        client_id: Optional[str] = None,
        symbol: Optional[str] = None,
        trade_date: Optional[date] = None,
    ) -> Tuple[InstitutionalTrade, ...]:
        symbol_value = symbol.upper().replace("/", "") if symbol else None
        with self._lock:
            values = tuple(
                item
                for item in self._trades.values()
                if (client_id is None or item.client_id == client_id)
                and (symbol_value is None or item.symbol == symbol_value)
                and (trade_date is None or item.trade_date == trade_date)
            )
            return tuple(sorted(values, key=lambda item: (item.booked_at, item.trade_id)))

    def dealer_statistics(self, dealer_id: str) -> dict[str, object]:
        requests = self.list_requests(dealer_id=dealer_id)
        quoted = [item for item in requests if item.quoted_at is not None]
        accepted = [item for item in requests if item.status == RfqStatus.ACCEPTED]
        response_seconds = [
            Decimal(str((item.quoted_at - item.requested_at).total_seconds()))
            for item in quoted
            if item.quoted_at is not None
        ]
        average_response = (
            sum(response_seconds, Decimal("0")) / len(response_seconds)
            if response_seconds
            else None
        )
        hit_rate = Decimal(len(accepted)) / len(quoted) if quoted else Decimal("0")
        return {
            "dealer_id": dealer_id,
            "request_count": len(requests),
            "quote_count": len(quoted),
            "accepted_count": len(accepted),
            "hit_rate": hit_rate,
            "average_response_seconds": average_response,
        }

    def _terminal(
        self,
        rfq_id: str,
        target: RfqStatus,
        expected_version: int,
    ) -> QuoteRequest:
        with self._lock:
            current = self._require(rfq_id)
            self._check_version(current, expected_version)
            if current.status not in {RfqStatus.OPEN, RfqStatus.QUOTED}:
                raise ValidationError("RFQ is already terminal")
            changed = replace(current, status=target, version=current.version + 1)
            self._requests[current.rfq_id] = changed
            return changed

    @staticmethod
    def _trade_amounts(request: QuoteRequest) -> Tuple[Decimal, Decimal]:
        if request.quoted_price is None:
            raise ValidationError("quoted price is required")
        if request.notional_currency == request.symbol[:3]:
            return request.notional, request.notional * request.quoted_price
        if request.notional_currency == request.symbol[3:]:
            return request.notional / request.quoted_price, request.notional
        raise ValidationError("notional currency must belong to the currency pair")

    def _require(self, rfq_id: str) -> QuoteRequest:
        key = required(rfq_id, "RFQ id")
        value = self._requests.get(key)
        if value is None:
            raise NotFoundError("RFQ does not exist")
        return value

    @staticmethod
    def _check_version(value: QuoteRequest, expected: int) -> None:
        if value.version != expected:
            raise StorageConflictError("RFQ version does not match")

    @staticmethod
    def _request_identity(value: QuoteRequest) -> tuple[object, ...]:
        return (
            value.client_id,
            value.symbol,
            value.side,
            value.notional,
            value.notional_currency,
            value.settlement_date,
            value.requested_at,
            value.expires_at,
        )
