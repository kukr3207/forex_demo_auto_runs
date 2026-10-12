"""Thread-safe vendor and contract lifecycle registry."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from threading import RLock
from typing import Dict, Iterable, Optional, Tuple
from uuid import uuid4

from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError
from forex_monitor.third_party.models import (
    AuditRecord,
    ContractStatus,
    Criticality,
    ServiceContract,
    Vendor,
    VendorStatus,
    required,
    utc,
)


class VendorRegistry:
    """Owns vendor identity, approvals, dependencies, and agreements."""

    def __init__(self) -> None:
        self._vendors: Dict[str, Vendor] = {}
        self._contracts: Dict[str, ServiceContract] = {}
        self._legal_names: Dict[str, str] = {}
        self._dependencies: Dict[str, Tuple[str, ...]] = {}
        self._audit: list[AuditRecord] = []
        self._lock = RLock()

    def register(self, vendor: Vendor, *, actor: str) -> Vendor:
        if not isinstance(vendor, Vendor):
            raise ValidationError("vendor must be Vendor")
        with self._lock:
            existing = self._vendors.get(vendor.vendor_id)
            if existing is not None:
                if existing == vendor:
                    return existing
                raise StorageConflictError("vendor id already exists")
            name_key = vendor.legal_name.casefold()
            if name_key in self._legal_names:
                raise StorageConflictError("vendor legal name already exists")
            self._vendors[vendor.vendor_id] = vendor
            self._legal_names[name_key] = vendor.vendor_id
            self._record(vendor.vendor_id, "registered", actor, vendor.created_at)
            return vendor

    def submit_for_review(self, vendor_id: str, *, actor: str, at: datetime, expected_version: int) -> Vendor:
        return self._transition(
            vendor_id,
            from_states=(VendorStatus.DRAFT,),
            to_state=VendorStatus.UNDER_REVIEW,
            actor=actor,
            at=at,
            expected_version=expected_version,
        )

    def approve(self, vendor_id: str, *, actor: str, at: datetime, expected_version: int) -> Vendor:
        instant = utc(at, "approval time")
        with self._lock:
            current = self._require_vendor(vendor_id)
            self._check_version(current.version, expected_version)
            if current.status != VendorStatus.UNDER_REVIEW:
                raise ValidationError("only vendors under review can be approved")
            changed = replace(
                current,
                status=VendorStatus.APPROVED,
                approved_at=instant,
                version=current.version + 1,
            )
            self._vendors[vendor_id] = changed
            self._record(vendor_id, "approved", actor, instant)
            return changed

    def restrict(
        self,
        vendor_id: str,
        *,
        reason: str,
        actor: str,
        at: datetime,
        expected_version: int,
    ) -> Vendor:
        reason_text = required(reason, "restriction reason", 500)
        instant = utc(at, "restriction time")
        with self._lock:
            current = self._require_vendor(vendor_id)
            self._check_version(current.version, expected_version)
            if current.status not in {VendorStatus.APPROVED, VendorStatus.UNDER_REVIEW}:
                raise ValidationError("vendor cannot be restricted from its current state")
            changed = replace(current, status=VendorStatus.RESTRICTED, version=current.version + 1)
            self._vendors[vendor_id] = changed
            self._record(vendor_id, "restricted", actor, instant, {"reason": reason_text})
            return changed

    def offboard(
        self,
        vendor_id: str,
        *,
        actor: str,
        at: datetime,
        expected_version: int,
        force: bool = False,
    ) -> Vendor:
        instant = utc(at, "offboarding time")
        with self._lock:
            current = self._require_vendor(vendor_id)
            self._check_version(current.version, expected_version)
            active_contracts = self.contracts(vendor_id=vendor_id, active_on=instant.date())
            dependents = tuple(key for key, dependencies in self._dependencies.items() if vendor_id in dependencies)
            if not force and active_contracts:
                raise ValidationError("vendor has active contracts")
            if not force and dependents:
                raise ValidationError("vendor is required by another vendor")
            changed = replace(
                current,
                status=VendorStatus.OFFBOARDED,
                offboarded_at=instant,
                version=current.version + 1,
            )
            self._vendors[vendor_id] = changed
            self._record(vendor_id, "offboarded", actor, instant, {"forced": str(force).lower()})
            return changed

    def set_dependencies(
        self,
        vendor_id: str,
        dependency_ids: Iterable[str],
        *,
        actor: str,
        at: datetime,
    ) -> Tuple[str, ...]:
        dependencies = tuple(dict.fromkeys(required(item, "dependency id") for item in dependency_ids))
        with self._lock:
            self._require_vendor(vendor_id)
            if vendor_id in dependencies:
                raise ValidationError("vendor cannot depend on itself")
            for dependency_id in dependencies:
                dependency = self._require_vendor(dependency_id)
                if dependency.status == VendorStatus.OFFBOARDED:
                    raise ValidationError("dependency is offboarded")
                if self._reaches(dependency_id, vendor_id):
                    raise ValidationError("vendor dependency graph would contain a cycle")
            self._dependencies[vendor_id] = dependencies
            self._record(vendor_id, "dependencies_changed", actor, utc(at, "change time"), {"count": str(len(dependencies))})
            return dependencies

    def add_contract(self, contract: ServiceContract, *, actor: str, at: datetime) -> ServiceContract:
        if not isinstance(contract, ServiceContract):
            raise ValidationError("contract must be ServiceContract")
        instant = utc(at, "contract creation time")
        with self._lock:
            vendor = self._require_vendor(contract.vendor_id)
            if vendor.status == VendorStatus.OFFBOARDED:
                raise ValidationError("offboarded vendor cannot receive a contract")
            existing = self._contracts.get(contract.contract_id)
            if existing is not None:
                if existing == contract:
                    return existing
                raise StorageConflictError("contract id already exists")
            for item in self._contracts.values():
                if item.vendor_id != contract.vendor_id or item.service.casefold() != contract.service.casefold():
                    continue
                if item.status != ContractStatus.TERMINATED and contract.starts_on < item.ends_on and item.starts_on < contract.ends_on:
                    raise ValidationError("contract dates overlap for this vendor service")
            self._contracts[contract.contract_id] = contract
            self._record(contract.vendor_id, "contract_added", actor, instant, {"contract_id": contract.contract_id})
            return contract

    def activate_contract(
        self,
        contract_id: str,
        *,
        actor: str,
        at: datetime,
        expected_version: int,
    ) -> ServiceContract:
        instant = utc(at, "activation time")
        with self._lock:
            current = self._require_contract(contract_id)
            self._check_version(current.version, expected_version)
            vendor = self._require_vendor(current.vendor_id)
            if vendor.status != VendorStatus.APPROVED:
                raise ValidationError("contract vendor is not approved")
            if current.status != ContractStatus.DRAFT:
                raise ValidationError("only draft contracts can be activated")
            changed = replace(current, status=ContractStatus.ACTIVE, activated_at=instant, version=current.version + 1)
            self._contracts[contract_id] = changed
            self._record(current.vendor_id, "contract_activated", actor, instant, {"contract_id": contract_id})
            return changed

    def terminate_contract(
        self,
        contract_id: str,
        *,
        actor: str,
        at: datetime,
        expected_version: int,
    ) -> ServiceContract:
        instant = utc(at, "termination time")
        with self._lock:
            current = self._require_contract(contract_id)
            self._check_version(current.version, expected_version)
            if current.status not in {ContractStatus.DRAFT, ContractStatus.ACTIVE}:
                raise ValidationError("contract cannot be terminated from its current state")
            changed = replace(current, status=ContractStatus.TERMINATED, terminated_at=instant, version=current.version + 1)
            self._contracts[contract_id] = changed
            self._record(current.vendor_id, "contract_terminated", actor, instant, {"contract_id": contract_id})
            return changed

    def expire_contracts(self, *, as_of: date, actor: str, at: datetime) -> Tuple[ServiceContract, ...]:
        instant = utc(at, "expiry time")
        changed: list[ServiceContract] = []
        with self._lock:
            for contract_id, current in tuple(self._contracts.items()):
                if current.status == ContractStatus.ACTIVE and current.ends_on <= as_of:
                    expired = replace(current, status=ContractStatus.EXPIRED, version=current.version + 1)
                    self._contracts[contract_id] = expired
                    self._record(current.vendor_id, "contract_expired", actor, instant, {"contract_id": contract_id})
                    changed.append(expired)
        return tuple(changed)

    def get(self, vendor_id: str) -> Vendor:
        with self._lock:
            return self._require_vendor(vendor_id)

    def get_contract(self, contract_id: str) -> ServiceContract:
        with self._lock:
            return self._require_contract(contract_id)

    def vendors(
        self,
        *,
        status: Optional[VendorStatus] = None,
        criticality: Optional[Criticality] = None,
    ) -> Tuple[Vendor, ...]:
        with self._lock:
            values = tuple(self._vendors.values())
        if status is not None:
            target_status = VendorStatus(status)
            values = tuple(item for item in values if item.status == target_status)
        if criticality is not None:
            target_criticality = Criticality(criticality)
            values = tuple(item for item in values if item.criticality == target_criticality)
        return tuple(sorted(values, key=lambda item: (item.legal_name.casefold(), item.vendor_id)))

    def contracts(
        self,
        *,
        vendor_id: Optional[str] = None,
        active_on: Optional[date] = None,
    ) -> Tuple[ServiceContract, ...]:
        values = tuple(self._contracts.values())
        if vendor_id is not None:
            values = tuple(item for item in values if item.vendor_id == vendor_id)
        if active_on is not None:
            values = tuple(
                item
                for item in values
                if item.status == ContractStatus.ACTIVE and item.starts_on <= active_on < item.ends_on
            )
        return tuple(sorted(values, key=lambda item: (item.starts_on, item.contract_id)))

    def dependencies(self, vendor_id: str, *, transitive: bool = False) -> Tuple[str, ...]:
        with self._lock:
            self._require_vendor(vendor_id)
            direct = self._dependencies.get(vendor_id, ())
            if not transitive:
                return direct
            found: set[str] = set()
            pending = list(direct)
            while pending:
                candidate = pending.pop(0)
                if candidate in found:
                    continue
                found.add(candidate)
                pending.extend(self._dependencies.get(candidate, ()))
            return tuple(sorted(found))

    def renewal_candidates(self, *, as_of: date, within_days: int) -> Tuple[ServiceContract, ...]:
        if within_days < 0:
            raise ValidationError("renewal window cannot be negative")
        return tuple(
            contract
            for contract in self.contracts()
            if contract.status == ContractStatus.ACTIVE
            and 0 <= (contract.ends_on - as_of).days <= within_days
        )

    def audit_trail(self, vendor_id: str) -> Tuple[AuditRecord, ...]:
        with self._lock:
            self._require_vendor(vendor_id)
            return tuple(item for item in self._audit if item.vendor_id == vendor_id)

    def _transition(
        self,
        vendor_id: str,
        *,
        from_states: Tuple[VendorStatus, ...],
        to_state: VendorStatus,
        actor: str,
        at: datetime,
        expected_version: int,
    ) -> Vendor:
        instant = utc(at, "transition time")
        with self._lock:
            current = self._require_vendor(vendor_id)
            self._check_version(current.version, expected_version)
            if current.status not in from_states:
                raise ValidationError("vendor cannot move from its current state")
            changed = replace(current, status=to_state, version=current.version + 1)
            self._vendors[vendor_id] = changed
            self._record(vendor_id, to_state.value, actor, instant)
            return changed

    def _reaches(self, start: str, target: str) -> bool:
        pending = [start]
        seen: set[str] = set()
        while pending:
            item = pending.pop()
            if item == target:
                return True
            if item not in seen:
                seen.add(item)
                pending.extend(self._dependencies.get(item, ()))
        return False

    def _require_vendor(self, vendor_id: str) -> Vendor:
        try:
            return self._vendors[required(vendor_id, "vendor id")]
        except KeyError as exc:
            raise NotFoundError("vendor does not exist") from exc

    def _require_contract(self, contract_id: str) -> ServiceContract:
        try:
            return self._contracts[required(contract_id, "contract id")]
        except KeyError as exc:
            raise NotFoundError("contract does not exist") from exc

    @staticmethod
    def _check_version(current: int, expected: int) -> None:
        if current != expected:
            raise StorageConflictError("record version does not match")

    def _record(
        self,
        vendor_id: str,
        action: str,
        actor: str,
        at: datetime,
        details: Optional[Dict[str, str]] = None,
    ) -> None:
        self._audit.append(AuditRecord(uuid4().hex, vendor_id, action, actor, at, details or {}))
