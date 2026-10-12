from datetime import datetime, timedelta, timezone

import pytest

from forex_monitor.continuity.backups import BackupCatalog
from forex_monitor.continuity.failover import FailoverCoordinator
from forex_monitor.continuity.models import BackupArtifact, BackupState, BackupType, FailoverGroup, FailoverState, RecoveryDrill, RecoveryObjective, RegionStatus
from forex_monitor.continuity.recovery import RecoveryPlanner
from forex_monitor.errors import ValidationError


NOW = datetime(2026, 10, 12, 8, tzinfo=timezone.utc)


def artifact(backup_id, kind, start, end, base=None):
    return BackupArtifact(backup_id, "database", kind, BackupState.AVAILABLE, NOW + timedelta(minutes=start), NOW + timedelta(minutes=start + 1), "s3://backups/%s" % backup_id, backup_id[0] * 64, 100, end if kind != BackupType.FULL else 1, end, base, key_id="key")


def catalog():
    value = BackupCatalog()
    value.register(artifact("aaaa", BackupType.FULL, 0, 1))
    value.register(artifact("bbbb", BackupType.INCREMENTAL, 10, 2, "aaaa"))
    value.register(artifact("cccc", BackupType.LOG, 20, 3, "bbbb"))
    return value


def test_backup_catalog_builds_contiguous_restore_chain() -> None:
    chain = catalog().restore_chain("database", NOW + timedelta(hours=1))
    assert [item.backup_id for item in chain] == ["aaaa", "bbbb", "cccc"]


def test_corrupt_backup_is_excluded_from_chain() -> None:
    backups = catalog()
    backups.mark_corrupt("bbbb")
    chain = backups.restore_chain("database", NOW + timedelta(hours=1))
    assert [item.backup_id for item in chain] == ["aaaa"]


def test_restore_plan_validates_runs_and_completes() -> None:
    planner = RecoveryPlanner(catalog())
    plan = planner.plan(plan_id="plan", resource_id="database", target_time=NOW + timedelta(hours=1), target_environment="recovery", created_at=NOW, created_by="ops")
    validated = planner.validate(plan.plan_id, expected_version=1)
    running = planner.start(plan.plan_id, at=NOW, expected_version=validated.version)
    verifying = planner.begin_verification(plan.plan_id, expected_version=running.version)
    complete = planner.complete(plan.plan_id, checks={"rows": True, "checksums": True}, at=NOW + timedelta(hours=1), expected_version=verifying.version)
    assert complete.state.value == "completed"


def test_recovery_drill_compares_rpo_and_rto() -> None:
    planner = RecoveryPlanner(catalog())
    planner.set_objective(RecoveryObjective("database", timedelta(minutes=5), timedelta(minutes=30)))
    drill = RecoveryDrill("drill", "database", NOW, NOW + timedelta(minutes=20), NOW - timedelta(minutes=2), 120, 1200, (("queries", True),))
    _, passed = planner.record_drill(drill)
    assert passed


def test_failover_requires_healthy_candidate_and_two_approvals() -> None:
    coordinator = FailoverCoordinator()
    group = coordinator.add_group(FailoverGroup("group", "database", "primary", ("secondary",)))
    coordinator.observe(group.group_id, RegionStatus("primary", False, NOW, 0, False))
    coordinator.observe(group.group_id, RegionStatus("secondary", True, NOW, 2, True))
    current = coordinator.approve(group.group_id, "alice")
    with pytest.raises(ValidationError):
        coordinator.failover(group.group_id, target_region="secondary", expected_version=current.version)
    coordinator.approve(group.group_id, "bob")
    failed_over = coordinator.failover(group.group_id, target_region="secondary", expected_version=current.version)
    assert failed_over.state == FailoverState.FAILED_OVER
    assert failed_over.active_region == "secondary"
