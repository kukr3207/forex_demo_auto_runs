"""Treaty-aware withholding with certification and backup rates."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from threading import RLock
from typing import Dict, Iterable, Optional, Tuple

from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError
from forex_monitor.tax.models import IncomeType, TaxProfile, WithholdingResult, WithholdingRule, currency, number, text, utc


class WithholdingEngine:
    def __init__(self, *, backup_rate: object = "0.24") -> None:
        self.backup_rate = number(backup_rate, "backup rate", non_negative=True)
        if self.backup_rate > 1:
            raise ValidationError("backup rate cannot exceed one")
        self._profiles: Dict[str, TaxProfile] = {}
        self._client_profiles: Dict[str, str] = {}
        self._rules: Dict[str, WithholdingRule] = {}
        self._calculations: Dict[str, WithholdingResult] = {}
        self._lock = RLock()

    def add_profile(self, profile: TaxProfile) -> TaxProfile:
        with self._lock:
            existing = self._profiles.get(profile.profile_id)
            if existing is not None:
                if existing == profile:
                    return existing
                raise StorageConflictError("tax profile id already exists")
            current = self._client_profiles.get(profile.client_id)
            if current:
                raise StorageConflictError("client already has a tax profile")
            self._profiles[profile.profile_id] = profile
            self._client_profiles[profile.client_id] = profile.profile_id
            return profile

    def add_rule(self, rule: WithholdingRule) -> WithholdingRule:
        with self._lock:
            existing = self._rules.get(rule.rule_id)
            if existing is not None:
                if existing == rule:
                    return existing
                raise StorageConflictError("withholding rule id already exists")
            for item in self._rules.values():
                if item.source_country == rule.source_country and item.income_type == rule.income_type and self._overlaps(item, rule):
                    raise StorageConflictError("withholding rules overlap")
            self._rules[rule.rule_id] = rule
            return rule

    def calculate(
        self,
        *,
        calculation_id: str,
        client_id: str,
        source_country: str,
        income_type: IncomeType,
        gross_amount: object,
        currency_code: str,
        calculated_at: datetime,
    ) -> WithholdingResult:
        instant = utc(calculated_at, "calculated at")
        gross = number(gross_amount, "gross amount", non_negative=True)
        with self._lock:
            existing = self._calculations.get(calculation_id)
            if existing is not None:
                return existing
            profile_id = self._client_profiles.get(client_id)
            if profile_id is None:
                raise NotFoundError("client tax profile does not exist")
            profile = self._profiles[profile_id]
            rules = [item for item in self._rules.values() if item.source_country == source_country.upper() and item.income_type == IncomeType(income_type) and item.effective_on(instant.date())]
            if len(rules) != 1:
                raise ValidationError("exactly one withholding rule must apply")
            rule = rules[0]
            certified = profile.certified_at is not None and profile.certified_at <= instant and (profile.expires_at is None or instant < profile.expires_at)
            if profile.exempt and certified:
                rate = Decimal("0")
                reason = "certified exemption"
            elif profile.backup_withholding or not certified:
                rate = self.backup_rate
                reason = "backup withholding"
            elif profile.treaty_country and profile.treaty_country in dict(rule.treaty_rates):
                rate = dict(rule.treaty_rates)[profile.treaty_country]
                reason = "treaty rate"
            elif profile.entity_type in dict(rule.entity_rates):
                rate = dict(rule.entity_rates)[profile.entity_type]
                reason = "entity rate"
            else:
                rate = rule.default_rate
                reason = "statutory rate"
            withheld = (gross * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            result = WithholdingResult(calculation_id, client_id, IncomeType(income_type), gross, rate, withheld, gross - withheld, currency(currency_code), instant, rule.rule_id, reason)
            self._calculations[calculation_id] = result
            return result

    def calculations(self, client_id: str, *, year: Optional[int] = None) -> Tuple[WithholdingResult, ...]:
        with self._lock:
            values = [item for item in self._calculations.values() if item.client_id == client_id and (year is None or item.calculated_at.year == year)]
            return tuple(sorted(values, key=lambda item: (item.calculated_at, item.calculation_id)))

    @staticmethod
    def _overlaps(left: WithholdingRule, right: WithholdingRule) -> bool:
        return (left.effective_until is None or right.effective_from <= left.effective_until) and (right.effective_until is None or left.effective_from <= right.effective_until)
