from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from forex_monitor.errors import StorageConflictError, ValidationError
from forex_monitor.third_party import (
    Contact,
    ContractStatus,
    Criticality,
    IncidentManager,
    IncidentSeverity,
    IncidentStatus,
    Measurement,
    MetricDirection,
    MetricKind,
    ReviewStatus,
    RiskAssessment,
    RiskFinding,
    RiskManager,
    ServiceContract,
    ServiceLevel,
    ServiceLevelManager,
    Vendor,
    VendorIncident,
    VendorRegistry,
    VendorStatus,
)


NOW = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)


def vendor(vendor_id: str = "vendor-1", *, country: str = "US", service: str = "Market Data") -> Vendor:
    return Vendor(
        vendor_id=vendor_id,
        legal_name="%s Incorporated" % vendor_id,
        service_name=service,
        country=country,
        criticality=Criticality.CRITICAL,
        owner="market-operations",
        contacts=(Contact("Service Desk", "%s@example.com" % vendor_id, "operations"),),
        created_at=NOW,
        data_categories=("market-data", "telemetry"),
        services=(service,),
    )


def availability() -> ServiceLevel:
    return ServiceLevel(
        metric_id="availability",
        name="Feed availability",
        kind=MetricKind.AVAILABILITY,
        direction=MetricDirection.AT_LEAST,
        target=Decimal("0.999"),
        unit="ratio",
        credit_rate=Decimal("0.10"),
        credit_cap=Decimal("0.15"),
    )


def latency() -> ServiceLevel:
    return ServiceLevel(
        metric_id="latency",
        name="Tick latency",
        kind=MetricKind.LATENCY,
        direction=MetricDirection.AT_MOST,
        target=Decimal("100"),
        unit="milliseconds",
    )


def contract(vendor_id: str = "vendor-1") -> ServiceContract:
    return ServiceContract(
        contract_id="contract-%s" % vendor_id,
        vendor_id=vendor_id,
        service="Market Data",
        starts_on=date(2026, 1, 1),
        ends_on=date(2027, 1, 1),
        currency="usd",
        annual_value=Decimal("120000"),
        service_levels=(availability(), latency()),
    )


def approved_registry() -> VendorRegistry:
    registry = VendorRegistry()
    registered = registry.register(vendor(), actor="alice")
    reviewed = registry.submit_for_review(
        registered.vendor_id,
        actor="alice",
        at=NOW + timedelta(minutes=1),
        expected_version=registered.version,
    )
    registry.approve(
        reviewed.vendor_id,
        actor="risk",
        at=NOW + timedelta(minutes=2),
        expected_version=reviewed.version,
    )
    return registry


def test_vendor_and_contract_lifecycle_preserves_audit_history() -> None:
    registry = approved_registry()
    draft = registry.add_contract(contract(), actor="legal", at=NOW + timedelta(minutes=3))
    active = registry.activate_contract(
        draft.contract_id,
        actor="legal",
        at=NOW + timedelta(minutes=4),
        expected_version=draft.version,
    )

    assert registry.get("vendor-1").status == VendorStatus.APPROVED
    assert active.status == ContractStatus.ACTIVE
    assert registry.contracts(vendor_id="vendor-1", active_on=date(2026, 6, 1)) == (active,)
    assert [record.action for record in registry.audit_trail("vendor-1")] == [
        "registered",
        "under_review",
        "approved",
        "contract_added",
        "contract_activated",
    ]

    with pytest.raises(ValidationError, match="active contracts"):
        registry.offboard("vendor-1", actor="risk", at=NOW + timedelta(days=1), expected_version=3)


def test_dependency_graph_rejects_cycles_and_blocks_offboarding() -> None:
    registry = approved_registry()
    secondary = vendor("vendor-2", country="GB", service="Reference Data")
    registry.register(secondary, actor="alice")
    registry.set_dependencies("vendor-1", ("vendor-2",), actor="alice", at=NOW + timedelta(hours=1))

    assert registry.dependencies("vendor-1", transitive=True) == ("vendor-2",)
    with pytest.raises(ValidationError, match="cycle"):
        registry.set_dependencies("vendor-2", ("vendor-1",), actor="alice", at=NOW + timedelta(hours=2))
    with pytest.raises(ValidationError, match="required"):
        registry.offboard("vendor-2", actor="alice", at=NOW + timedelta(hours=3), expected_version=1)


def test_registry_uses_optimistic_versions() -> None:
    registry = approved_registry()
    with pytest.raises(StorageConflictError, match="version"):
        registry.restrict(
            "vendor-1",
            reason="control gap",
            actor="risk",
            at=NOW + timedelta(hours=1),
            expected_version=2,
        )


def test_service_levels_weight_ratios_and_calculate_credit() -> None:
    manager = ServiceLevelManager()
    agreement = manager.register_contract(contract())
    first = Measurement(
        "m1",
        agreement.contract_id,
        "availability",
        NOW,
        NOW + timedelta(hours=1),
        Decimal("0.998"),
        900,
        "prometheus",
    )
    second = Measurement(
        "m2",
        agreement.contract_id,
        "availability",
        NOW + timedelta(hours=1),
        NOW + timedelta(hours=2),
        Decimal("1"),
        100,
        "prometheus",
    )
    manager.record(first)
    manager.record(second)
    evaluation = manager.evaluate(
        agreement.contract_id,
        "availability",
        start=NOW,
        end=NOW + timedelta(hours=2),
    )
    credit = manager.credit(
        agreement.contract_id,
        "availability",
        start=NOW,
        end=NOW + timedelta(hours=2),
        eligible_amount=Decimal("10000"),
    )

    assert evaluation.observed == Decimal("0.998200")
    assert evaluation.met is False
    assert credit.credit_amount == Decimal("1000.00")
    assert manager.coverage(
        agreement.contract_id,
        "availability",
        start=NOW,
        end=NOW + timedelta(hours=2),
    ) == Decimal("1.000000")


def test_latency_uses_weighted_ninety_fifth_percentile() -> None:
    manager = ServiceLevelManager()
    agreement = manager.register_contract(contract())
    for index, (observed, samples) in enumerate((("70", 90), ("130", 10))):
        start = NOW + timedelta(minutes=index)
        manager.record(
            Measurement(
                "latency-%d" % index,
                agreement.contract_id,
                "latency",
                start,
                start + timedelta(minutes=1),
                Decimal(observed),
                samples,
                "gateway",
            )
        )

    result = manager.evaluate(
        agreement.contract_id,
        "latency",
        start=NOW,
        end=NOW + timedelta(minutes=2),
    )
    assert result.observed == Decimal("130.000000")
    assert result.met is False


def assessment(*, serious: bool = False) -> RiskAssessment:
    findings = ()
    if serious:
        findings = (
            RiskFinding(
                "finding-1",
                "resilience",
                "Recovery test is overdue",
                Criticality.HIGH,
                "Complete an observed recovery exercise",
                date(2026, 11, 1),
            ),
        )
    return RiskAssessment(
        "assessment-1",
        "vendor-1",
        date(2026, 10, 1),
        date(2027, 10, 1),
        "risk@example.com",
        85,
        70,
        40,
        findings,
    )


def test_risk_approval_requires_serious_findings_to_be_resolved() -> None:
    manager = RiskManager()
    opened = manager.submit(assessment(serious=True))
    with pytest.raises(ValidationError, match="findings"):
        manager.decide(opened.assessment_id, status=ReviewStatus.APPROVED, notes="approved", expected_version=1)
    remediated = manager.resolve_finding(
        opened.assessment_id,
        "finding-1",
        resolved_at=NOW,
        expected_version=1,
    )
    approved = manager.decide(
        remediated.assessment_id,
        status=ReviewStatus.APPROVED,
        notes="evidence accepted",
        expected_version=remediated.version,
    )
    assert approved.status == ReviewStatus.APPROVED
    assert approved.open_findings == ()


def test_portfolio_reports_country_and_service_concentration() -> None:
    manager = RiskManager()
    manager.submit(assessment())
    records = (
        vendor("vendor-1", country="US"),
        vendor("vendor-2", country="US"),
        vendor("vendor-3", country="GB", service="Reference Data"),
    )
    report = manager.portfolio(
        records,
        as_of=date(2026, 10, 12),
        annual_spend={"vendor-1": Decimal("70"), "vendor-2": Decimal("20"), "vendor-3": Decimal("10")},
    )
    assert report.vendor_count == 3
    assert report.overdue_review_count == 2
    assert report.highest_country_share == Decimal("0.9")
    assert "country concentration exceeds limit" in report.concentration_warnings


def open_incident() -> VendorIncident:
    return VendorIncident(
        "incident-1",
        "vendor-1",
        "Market Data",
        IncidentSeverity.CRITICAL,
        "Primary price feed unavailable",
        NOW,
        external_reference="provider-8842",
    )


def test_incident_workflow_tracks_deadlines_and_resolution_metrics() -> None:
    manager = IncidentManager()
    opened = manager.open(open_incident())
    breach = manager.deadline(opened.incident_id, as_of=NOW + timedelta(minutes=20))
    assert breach.acknowledgement_breached is True
    acknowledged = manager.acknowledge(opened.incident_id, at=NOW + timedelta(minutes=10), expected_version=1)
    mitigating = manager.add_update(
        acknowledged.incident_id,
        message="Traffic moved to backup feed",
        at=NOW + timedelta(minutes=20),
        expected_version=acknowledged.version,
        mitigating=True,
    )
    resolved = manager.resolve(
        mitigating.incident_id,
        resolution="Primary feed recovered and reconciled",
        at=NOW + timedelta(hours=1),
        expected_version=mitigating.version,
    )
    closed = manager.close(resolved.incident_id, expected_version=resolved.version)

    assert closed.status == IncidentStatus.CLOSED
    assert manager.mean_time_to_acknowledge() == timedelta(minutes=10)
    assert manager.mean_time_to_resolve() == timedelta(hours=1)


def test_incident_external_reference_is_idempotent_but_not_mutable() -> None:
    manager = IncidentManager()
    original = open_incident()
    assert manager.open(original) == manager.open(original)
    changed = VendorIncident(
        "incident-2",
        original.vendor_id,
        original.service,
        original.severity,
        "Different report for the same provider case",
        original.detected_at,
        external_reference=original.external_reference,
    )
    with pytest.raises(StorageConflictError, match="external"):
        manager.open(changed)
