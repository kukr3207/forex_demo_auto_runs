"""Client onboarding, agreements, permissions, and hierarchy."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from threading import RLock
from typing import Dict, Iterable, Optional, Tuple

from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError
from forex_monitor.institutional.models import (
    AgreementStatus,
    ClientAccount,
    ClientStatus,
    LegalAgreement,
    required,
)


class ClientDirectory:
    """In-memory domain service suitable for durable adapter composition."""

    _CLIENT_TRANSITIONS = {
        ClientStatus.PROSPECT: {ClientStatus.ONBOARDING, ClientStatus.CLOSED},
        ClientStatus.ONBOARDING: {ClientStatus.ACTIVE, ClientStatus.SUSPENDED, ClientStatus.CLOSED},
        ClientStatus.ACTIVE: {ClientStatus.SUSPENDED, ClientStatus.CLOSED},
        ClientStatus.SUSPENDED: {ClientStatus.ACTIVE, ClientStatus.CLOSED},
        ClientStatus.CLOSED: set(),
    }
    _AGREEMENT_TRANSITIONS = {
        AgreementStatus.DRAFT: {AgreementStatus.REVIEW, AgreementStatus.TERMINATED},
        AgreementStatus.REVIEW: {AgreementStatus.DRAFT, AgreementStatus.EXECUTED, AgreementStatus.TERMINATED},
        AgreementStatus.EXECUTED: {AgreementStatus.TERMINATED, AgreementStatus.EXPIRED},
        AgreementStatus.TERMINATED: set(),
        AgreementStatus.EXPIRED: set(),
    }

    def __init__(self) -> None:
        self._clients: Dict[str, ClientAccount] = {}
        self._lei_index: Dict[str, str] = {}
        self._agreements: Dict[str, LegalAgreement] = {}
        self._client_agreements: Dict[str, list[str]] = {}
        self._lock = RLock()

    def create_client(self, client: ClientAccount) -> ClientAccount:
        if not isinstance(client, ClientAccount):
            raise ValidationError("client must be ClientAccount")
        with self._lock:
            if client.client_id in self._clients:
                existing = self._clients[client.client_id]
                if existing == client:
                    return existing
                raise StorageConflictError("client id already exists")
            owner = self._lei_index.get(client.lei)
            if owner is not None:
                raise StorageConflictError("LEI already belongs to client %s" % owner)
            if client.parent_id is not None:
                parent = self._clients.get(client.parent_id)
                if parent is None:
                    raise ValidationError("parent client does not exist")
                if parent.status == ClientStatus.CLOSED:
                    raise ValidationError("closed client cannot become a parent")
            self._clients[client.client_id] = client
            self._lei_index[client.lei] = client.client_id
            self._client_agreements[client.client_id] = []
            return client

    def update_client(
        self,
        client_id: str,
        *,
        expected_version: int,
        legal_name: Optional[str] = None,
        classification: Optional[str] = None,
        risk_rating: Optional[str] = None,
        settlement_accounts: Optional[Iterable[str]] = None,
    ) -> ClientAccount:
        with self._lock:
            current = self._require_client(client_id)
            self._check_version(current.version, expected_version, "client")
            changes = {"version": current.version + 1}
            if legal_name is not None:
                changes["legal_name"] = required(legal_name, "legal name")
            if classification is not None:
                changes["classification"] = required(classification, "classification").lower()
            if risk_rating is not None:
                changes["risk_rating"] = required(risk_rating, "risk rating").lower()
            if settlement_accounts is not None:
                values = tuple(sorted(set(required(item, "settlement account") for item in settlement_accounts)))
                changes["settlement_accounts"] = values
            changed = replace(current, **changes)
            self._clients[current.client_id] = changed
            return changed

    def transition_client(
        self,
        client_id: str,
        status: ClientStatus,
        *,
        expected_version: int,
    ) -> ClientAccount:
        target = ClientStatus(status)
        with self._lock:
            current = self._require_client(client_id)
            self._check_version(current.version, expected_version, "client")
            if target == current.status:
                return current
            if target not in self._CLIENT_TRANSITIONS[current.status]:
                raise ValidationError(
                    "client cannot move from %s to %s" % (current.status.value, target.value)
                )
            if target == ClientStatus.ACTIVE:
                agreements = self.agreements_for(client_id, on=date.today())
                if not any(item.status == AgreementStatus.EXECUTED for item in agreements):
                    raise ValidationError("active client requires an executed agreement")
            changed = replace(current, status=target, version=current.version + 1)
            self._clients[current.client_id] = changed
            return changed

    def grant_permission(
        self,
        client_id: str,
        product: str,
        *,
        expected_version: int,
    ) -> ClientAccount:
        permission = required(product, "product").lower()
        with self._lock:
            current = self._require_client(client_id)
            self._check_version(current.version, expected_version, "client")
            if current.status == ClientStatus.CLOSED:
                raise ValidationError("closed client cannot receive permissions")
            permissions = tuple(sorted(set(current.trading_permissions + (permission,))))
            changed = replace(
                current,
                trading_permissions=permissions,
                version=current.version + 1,
            )
            self._clients[current.client_id] = changed
            return changed

    def revoke_permission(
        self,
        client_id: str,
        product: str,
        *,
        expected_version: int,
    ) -> ClientAccount:
        permission = required(product, "product").lower()
        with self._lock:
            current = self._require_client(client_id)
            self._check_version(current.version, expected_version, "client")
            permissions = tuple(item for item in current.trading_permissions if item != permission)
            changed = replace(
                current,
                trading_permissions=permissions,
                version=current.version + 1,
            )
            self._clients[current.client_id] = changed
            return changed

    def add_agreement(self, agreement: LegalAgreement) -> LegalAgreement:
        if not isinstance(agreement, LegalAgreement):
            raise ValidationError("agreement must be LegalAgreement")
        with self._lock:
            self._require_client(agreement.client_id)
            existing = self._agreements.get(agreement.agreement_id)
            if existing is not None:
                if existing == agreement:
                    return existing
                raise StorageConflictError("agreement id already exists")
            self._agreements[agreement.agreement_id] = agreement
            self._client_agreements[agreement.client_id].append(agreement.agreement_id)
            return agreement

    def transition_agreement(
        self,
        agreement_id: str,
        status: AgreementStatus,
        *,
        expected_version: int,
        document_hash: Optional[str] = None,
        signed_by: Iterable[str] = (),
    ) -> LegalAgreement:
        target = AgreementStatus(status)
        with self._lock:
            current = self._require_agreement(agreement_id)
            self._check_version(current.version, expected_version, "agreement")
            if target == current.status:
                return current
            if target not in self._AGREEMENT_TRANSITIONS[current.status]:
                raise ValidationError(
                    "agreement cannot move from %s to %s"
                    % (current.status.value, target.value)
                )
            changes = {"status": target, "version": current.version + 1}
            if target == AgreementStatus.EXECUTED:
                signers = tuple(sorted(set(required(item, "signer") for item in signed_by)))
                if len(signers) < 2:
                    raise ValidationError("executed agreement requires both parties")
                if not document_hash:
                    raise ValidationError("executed agreement requires document hash")
                changes["signed_by"] = signers
                changes["document_hash"] = document_hash
            changed = replace(current, **changes)
            self._agreements[current.agreement_id] = changed
            return changed

    def client(self, client_id: str) -> Optional[ClientAccount]:
        with self._lock:
            return self._clients.get(client_id.strip())

    def by_lei(self, lei: str) -> Optional[ClientAccount]:
        with self._lock:
            client_id = self._lei_index.get(lei.strip().upper())
            return self._clients.get(client_id) if client_id else None

    def list_clients(
        self,
        *,
        status: Optional[ClientStatus] = None,
        country: Optional[str] = None,
        parent_id: Optional[str] = None,
        permission: Optional[str] = None,
    ) -> Tuple[ClientAccount, ...]:
        status_value = ClientStatus(status) if status is not None else None
        country_code = country.strip().upper() if country else None
        permission_name = permission.strip().lower() if permission else None
        with self._lock:
            values = tuple(
                item
                for item in self._clients.values()
                if (status_value is None or item.status == status_value)
                and (country_code is None or item.country == country_code)
                and (parent_id is None or item.parent_id == parent_id)
                and (
                    permission_name is None
                    or permission_name in item.trading_permissions
                )
            )
            return tuple(sorted(values, key=lambda item: (item.legal_name, item.client_id)))

    def agreements_for(
        self,
        client_id: str,
        *,
        on: Optional[date] = None,
        agreement_type: Optional[str] = None,
    ) -> Tuple[LegalAgreement, ...]:
        client_key = client_id.strip()
        type_name = agreement_type.strip().lower() if agreement_type else None
        with self._lock:
            identifiers = self._client_agreements.get(client_key, ())
            values = tuple(self._agreements[item] for item in identifiers)
            if type_name is not None:
                values = tuple(item for item in values if item.agreement_type == type_name)
            if on is not None:
                values = tuple(
                    item
                    for item in values
                    if item.effective_from <= on
                    and (item.effective_to is None or on <= item.effective_to)
                )
            return tuple(sorted(values, key=lambda item: (item.effective_from, item.agreement_id)))

    def effective_agreement(
        self,
        client_id: str,
        product: str,
        currency_code: str,
        on: date,
    ) -> Optional[LegalAgreement]:
        product_name = required(product, "product").lower()
        currency_name = required(currency_code, "currency").upper()
        candidates = self.agreements_for(client_id, on=on)
        matches = [
            item
            for item in candidates
            if item.is_effective(on)
            and (not item.products or product_name in item.products)
            and (not item.currencies or currency_name in item.currencies)
        ]
        return matches[-1] if matches else None

    def hierarchy(self, client_id: str) -> Tuple[ClientAccount, ...]:
        with self._lock:
            current = self._require_client(client_id)
            result = [current]
            visited = {current.client_id}
            while current.parent_id is not None:
                if current.parent_id in visited:
                    raise StorageConflictError("client hierarchy contains a cycle")
                current = self._require_client(current.parent_id)
                visited.add(current.client_id)
                result.append(current)
            return tuple(result)

    def permissions(self, client_id: str) -> Tuple[str, ...]:
        inherited = set()
        for client in self.hierarchy(client_id):
            if client.status in {ClientStatus.ACTIVE, ClientStatus.ONBOARDING}:
                inherited.update(client.trading_permissions)
        return tuple(sorted(inherited))

    def expire_agreements(self, on: date) -> Tuple[LegalAgreement, ...]:
        changed = []
        with self._lock:
            for agreement_id, current in list(self._agreements.items()):
                if (
                    current.status == AgreementStatus.EXECUTED
                    and current.effective_to is not None
                    and current.effective_to < on
                ):
                    value = replace(
                        current,
                        status=AgreementStatus.EXPIRED,
                        version=current.version + 1,
                    )
                    self._agreements[agreement_id] = value
                    changed.append(value)
        return tuple(sorted(changed, key=lambda item: item.agreement_id))

    def _require_client(self, client_id: str) -> ClientAccount:
        key = required(client_id, "client id")
        value = self._clients.get(key)
        if value is None:
            raise NotFoundError("client does not exist")
        return value

    def _require_agreement(self, agreement_id: str) -> LegalAgreement:
        key = required(agreement_id, "agreement id")
        value = self._agreements.get(key)
        if value is None:
            raise NotFoundError("agreement does not exist")
        return value

    @staticmethod
    def _check_version(actual: int, expected: int, subject: str) -> None:
        if actual != expected:
            raise StorageConflictError("%s version does not match" % subject)
