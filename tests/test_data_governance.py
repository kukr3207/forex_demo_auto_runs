from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from forex_monitor.data_governance.access import DataAccessService
from forex_monitor.data_governance.catalog import DataCatalog
from forex_monitor.data_governance.lineage import LineageGraph
from forex_monitor.data_governance.models import AccessPolicy, CheckSeverity, CheckType, Classification, Dataset, DatasetSchema, FieldType, LineageEdge, QualityCheck, SchemaField
from forex_monitor.data_governance.quality import DataQualityService
from forex_monitor.data_governance.privacy import Consent, ErasureEvidence, LegalHold, PrivacyService, RequestStatus, RequestType, SubjectRequest
from forex_monitor.data_governance.retention import RetentionAction, RetentionCandidate, RetentionPolicy, RetentionService
from forex_monitor.data_governance.schemas import SchemaRegistry
from forex_monitor.errors import StorageConflictError, ValidationError


NOW = datetime(2026, 10, 12, 8, tzinfo=timezone.utc)


def dataset(dataset_id="quotes"):
    return Dataset(dataset_id, dataset_id.title(), "market-data", "data-team", "Normalized market data", Classification.CONFIDENTIAL, "s3://lake/%s" % dataset_id, NOW, 365, tags=("fx", "market"))


def test_catalog_searches_metadata_and_enforces_unique_names() -> None:
    catalog = DataCatalog()
    value = catalog.register(dataset())
    assert catalog.register(value) == value
    assert catalog.search("normalized", tags=("fx",)) == (value,)
    with pytest.raises(StorageConflictError):
        catalog.register(Dataset("other", "Quotes", "market-data", "team", "Other", Classification.INTERNAL, "s3://other", NOW, 10))


def test_catalog_retention_and_optimistic_update() -> None:
    catalog = DataCatalog()
    catalog.register(dataset())
    changed = catalog.update("quotes", expected_version=1, retention_days=30, steward="alice")
    assert changed.version == 2
    assert catalog.retention_due(NOW + timedelta(days=31)) == (changed,)


def test_schema_registry_checks_backward_compatibility_and_rows() -> None:
    registry = SchemaRegistry()
    first = DatasetSchema("quotes-v1", "quotes", 1, (SchemaField("symbol", FieldType.STRING), SchemaField("price", FieldType.DECIMAL)), NOW, "alice")
    registry.register(first)
    second = DatasetSchema("quotes-v2", "quotes", 2, first.fields + (SchemaField("venue", FieldType.STRING, nullable=True),), NOW, "alice")
    registry.register(second)
    assert not registry.validate_row("quotes", {"symbol": "EURUSD", "price": "1.1", "venue": None})
    assert registry.validate_row("quotes", {"symbol": "EURUSD"}) == ("required field is missing: price",)


def test_schema_rejects_breaking_field_removal() -> None:
    registry = SchemaRegistry()
    registry.register(DatasetSchema("v1", "quotes", 1, (SchemaField("symbol", FieldType.STRING),), NOW, "alice"))
    with pytest.raises(ValidationError):
        registry.register(DatasetSchema("v2", "quotes", 2, (SchemaField("price", FieldType.DECIMAL, nullable=True),), NOW, "alice"))


def test_lineage_traverses_impact_and_prevents_cycles() -> None:
    graph = LineageGraph()
    graph.add(LineageEdge("a-b", "raw", "clean", "normalize", "job-1", NOW, (("bid", "bid"),)))
    graph.add(LineageEdge("b-c", "clean", "features", "aggregate", "job-2", NOW))
    assert graph.downstream("raw") == ("clean", "features")
    assert graph.upstream("features") == ("clean", "raw")
    assert graph.field_sources("clean", "bid") == (("raw", "bid"),)
    with pytest.raises(ValidationError):
        graph.add(LineageEdge("cycle", "features", "raw", "bad", "job-3", NOW))


def test_access_policy_requires_matching_purpose_and_masks_fields() -> None:
    service = DataAccessService(masking_salt="secret")
    service.add_policy(AccessPolicy("policy", "quotes", ("alice",), ("read", "export"), "research", masked_fields=("client_id",)))
    allowed = service.decide("alice", dataset(), "read", "research", at=NOW)
    denied = service.decide("alice", dataset(), "read", "marketing", at=NOW)
    assert allowed.allowed
    assert not denied.allowed
    masked = service.mask({"client_id": "client-1", "symbol": "EURUSD"}, allowed.masked_fields)
    assert masked["client_id"].startswith("masked:")
    assert masked["symbol"] == "EURUSD"


def test_access_events_are_idempotent_and_filterable() -> None:
    service = DataAccessService()
    service.add_policy(AccessPolicy("policy", "quotes", ("alice",), ("read",), "research"))
    decision = service.decide("alice", dataset(), "read", "research", at=NOW)
    event = service.record("event", decision, purpose="research", occurred_at=NOW, record_count=10)
    assert service.record("event", decision, purpose="research", occurred_at=NOW, record_count=10) == event
    assert service.events(principal="alice", allowed=True) == (event,)


def test_quality_checks_find_null_duplicate_range_and_allowed_value() -> None:
    service = DataQualityService()
    checks = (
        QualityCheck("not-null", "quotes", "Symbol present", CheckType.NOT_NULL, "symbol", CheckSeverity.ERROR),
        QualityCheck("unique", "quotes", "IDs unique", CheckType.UNIQUE, "id", CheckSeverity.ERROR),
        QualityCheck("range", "quotes", "Price positive", CheckType.RANGE, "price", CheckSeverity.ERROR, (("minimum", "0"),)),
        QualityCheck("venue", "quotes", "Venue allowed", CheckType.ALLOWED_VALUES, "venue", CheckSeverity.WARNING, (("values", "EBS|RTRS"),)),
    )
    for check in checks:
        service.add_check(check)
    rows = ({"id": 1, "symbol": "EURUSD", "price": 1, "venue": "EBS"}, {"id": 1, "symbol": None, "price": -1, "venue": "OTHER"})
    results = service.run("run", "quotes", rows, checked_at=NOW)
    assert all(not item.passed for item in results)
    assert {item.check_id: item.failed_rows for item in results}["unique"] == (1,)


def test_quality_freshness_and_row_count() -> None:
    service = DataQualityService()
    service.add_check(QualityCheck("fresh", "quotes", "Fresh", CheckType.FRESHNESS, "observed_at", CheckSeverity.ERROR, (("max_age_seconds", "60"),)))
    service.add_check(QualityCheck("count", "quotes", "Enough rows", CheckType.ROW_COUNT, None, CheckSeverity.ERROR, (("minimum", "2"),)))
    results = {item.check_id: item for item in service.run("run", "quotes", ({"observed_at": NOW - timedelta(seconds=30)},), checked_at=NOW)}
    assert results["fresh"].passed
    assert not results["count"].passed


def test_consent_history_uses_latest_effective_decision() -> None:
    service = PrivacyService()
    service.record_consent(Consent("yes", "subject", "analytics", True, NOW, NOW, "portal"))
    service.record_consent(Consent("no", "subject", "analytics", False, NOW + timedelta(hours=1), NOW + timedelta(hours=1), "portal", supersedes="yes"))
    assert service.has_consent("subject", "analytics", at=NOW + timedelta(minutes=30))
    assert not service.has_consent("subject", "analytics", at=NOW + timedelta(hours=2))


def test_legal_hold_blocks_erasure_until_released() -> None:
    service = PrivacyService()
    service.add_hold(LegalHold("hold", "subject", "litigation", NOW, "legal", ("quotes",)))
    request = service.receive_request(SubjectRequest("request", "subject", RequestType.ERASURE, NOW, NOW + timedelta(days=30), "EU"))
    verified = service.verify(request.request_id, verified_at=NOW, expected_version=1)
    blocked = service.start(request.request_id, datasets=("quotes",), expected_version=verified.version)
    assert blocked.status == RequestStatus.BLOCKED


def test_erasure_requires_evidence_for_every_dataset() -> None:
    service = PrivacyService()
    request = service.receive_request(SubjectRequest("request", "subject", RequestType.ERASURE, NOW, NOW + timedelta(days=30), "EU"))
    verified = service.verify(request.request_id, verified_at=NOW, expected_version=1)
    active = service.start(request.request_id, datasets=("quotes", "trades"), expected_version=verified.version)
    service.add_erasure_evidence(ErasureEvidence("e1", request.request_id, "quotes", 10, NOW, "job", "a" * 64))
    with pytest.raises(ValidationError):
        service.complete(request.request_id, completed_at=NOW, expected_version=active.version)
    service.add_erasure_evidence(ErasureEvidence("e2", request.request_id, "trades", 5, NOW, "job", "b" * 64))
    assert service.complete(request.request_id, completed_at=NOW, expected_version=active.version).status == RequestStatus.COMPLETED


def test_retention_plan_excludes_holds_and_accounts_for_execution() -> None:
    service = RetentionService()
    service.add_policy(RetentionPolicy("policy", "quotes", RetentionAction.DELETE, 30, "observed_at"))
    candidates = (
        RetentionCandidate("old", "2026-01", NOW - timedelta(days=40), size_bytes=100),
        RetentionCandidate("held", "2026-01", NOW - timedelta(days=40), subject_id="subject", size_bytes=200),
        RetentionCandidate("new", "2026-10", NOW - timedelta(days=1), size_bytes=300),
    )
    plan = service.plan(plan_id="plan", policy_id="policy", candidates=candidates, created_at=NOW, held_subject_ids=("subject",))
    assert plan.candidate_ids == ("old",)
    assert plan.excluded_ids == ("held",)
    execution = service.complete(execution_id="execution", plan_id=plan.plan_id, started_at=NOW, completed_at=NOW, processed_ids=("old",), failures={}, manifest_uri="s3://manifests/one")
    assert execution.success_count == 1
