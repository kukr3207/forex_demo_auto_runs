"""Configurable jurisdiction rules for reportability and field validation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Dict, Iterable, Mapping, Optional, Tuple

from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError
from forex_monitor.regulatory.models import AssetClass, TradeEconomics, TransactionReport, ValidationFinding, required


@dataclass(frozen=True)
class RegimeRule:
    regime: str
    countries: Tuple[str, ...]
    asset_classes: Tuple[AssetClass, ...]
    deadline: timedelta
    require_reporting_lei: bool = True
    require_counterparty_lei: bool = True
    require_venue: bool = False
    minimum_notional: Optional[Decimal] = None
    required_fields: Tuple[str, ...] = ()
    allowed_execution_methods: Tuple[str, ...] = ("electronic", "voice", "hybrid")

    def __post_init__(self) -> None:
        object.__setattr__(self, "regime", required(self.regime, "regime").upper())
        countries = tuple(sorted({required(item, "country").upper() for item in self.countries}))
        if any(len(item) != 2 for item in countries):
            raise ValidationError("regime countries must be ISO alpha-2")
        if not self.asset_classes:
            raise ValidationError("regime must cover an asset class")
        if self.deadline <= timedelta(0):
            raise ValidationError("report deadline must be positive")
        object.__setattr__(self, "countries", countries)
        object.__setattr__(self, "asset_classes", tuple(sorted({AssetClass(item) for item in self.asset_classes}, key=lambda item: item.value)))
        if self.minimum_notional is not None and self.minimum_notional < 0:
            raise ValidationError("minimum notional cannot be negative")
        object.__setattr__(self, "required_fields", tuple(sorted(set(self.required_fields))))
        object.__setattr__(self, "allowed_execution_methods", tuple(sorted(set(self.allowed_execution_methods))))


class ReportingRulebook:
    def __init__(self, rules: Iterable[RegimeRule] = ()) -> None:
        self._rules: Dict[str, RegimeRule] = {}
        for rule in rules:
            self.add(rule)

    def add(self, rule: RegimeRule) -> RegimeRule:
        if not isinstance(rule, RegimeRule):
            raise ValidationError("rule must be RegimeRule")
        existing = self._rules.get(rule.regime)
        if existing is not None and existing != rule:
            raise StorageConflictError("reporting regime already exists")
        self._rules[rule.regime] = rule
        return rule

    def rule(self, regime: str) -> RegimeRule:
        key = required(regime, "regime").upper()
        if key not in self._rules:
            raise NotFoundError("reporting regime does not exist")
        return self._rules[key]

    def applicable_regimes(self, economics: TradeEconomics, reporting_country: str) -> Tuple[str, ...]:
        country = required(reporting_country, "reporting country").upper()
        return tuple(
            sorted(
                rule.regime
                for rule in self._rules.values()
                if country in rule.countries
                and economics.asset_class in rule.asset_classes
                and (rule.minimum_notional is None or economics.quote_notional >= rule.minimum_notional)
            )
        )

    def validate(self, report: TransactionReport) -> Tuple[ValidationFinding, ...]:
        rule = self.rule(report.regime)
        findings = []
        if report.economics.asset_class not in rule.asset_classes:
            findings.append(ValidationFinding("assetClass", "ASSET_CLASS_NOT_REPORTABLE", "asset class is outside the regime"))
        if report.reporting_party.country not in rule.countries:
            findings.append(ValidationFinding("reportingParty.country", "JURISDICTION_MISMATCH", "reporting party is outside the regime"))
        if rule.require_reporting_lei and report.reporting_party.lei is None:
            findings.append(ValidationFinding("reportingParty.lei", "LEI_REQUIRED", "reporting party LEI is required"))
        if rule.require_counterparty_lei and report.counterparty.lei is None:
            findings.append(ValidationFinding("counterparty.lei", "LEI_REQUIRED", "counterparty LEI is required"))
        if rule.require_venue and not report.economics.venue:
            findings.append(ValidationFinding("venue", "VENUE_REQUIRED", "execution venue is required"))
        if report.economics.execution_method not in rule.allowed_execution_methods:
            findings.append(ValidationFinding("executionMethod", "EXECUTION_METHOD_INVALID", "execution method is not permitted"))
        if rule.minimum_notional is not None and report.economics.quote_notional < rule.minimum_notional:
            findings.append(ValidationFinding("notional", "BELOW_REPORTING_THRESHOLD", "trade is below the reporting threshold", "warning"))
        values = report.as_dict()
        for field in rule.required_fields:
            if values.get(field) in {None, ""}:
                findings.append(ValidationFinding(field, "REQUIRED_FIELD_MISSING", "required report field is missing"))
        if report.report_deadline and report.generated_at > report.report_deadline:
            findings.append(ValidationFinding("generatedAt", "REPORT_LATE", "report was generated after its deadline", "warning"))
        return tuple(findings)

    def deadline_for(self, regime: str, economics: TradeEconomics):
        return economics.traded_at + self.rule(regime).deadline

    @classmethod
    def standard(cls) -> "ReportingRulebook":
        return cls(
            (
                RegimeRule("EMIR", ("AT", "BE", "DE", "ES", "FR", "IE", "IT", "LU", "NL", "PT"), tuple(AssetClass), timedelta(days=1)),
                RegimeRule("UK-EMIR", ("GB",), tuple(AssetClass), timedelta(days=1)),
                RegimeRule("MIFIR", ("AT", "BE", "DE", "ES", "FR", "IE", "IT", "LU", "NL", "PT"), (AssetClass.SPOT, AssetClass.FORWARD, AssetClass.SWAP), timedelta(days=1), require_venue=True),
                RegimeRule("CFTC", ("US",), (AssetClass.FORWARD, AssetClass.SWAP, AssetClass.NDF, AssetClass.OPTION), timedelta(hours=24)),
            )
        )
