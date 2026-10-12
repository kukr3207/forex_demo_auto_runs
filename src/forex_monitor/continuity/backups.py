"""Backup-chain registration, expiry, and point-in-time selection."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from threading import RLock
from typing import Dict, Optional, Tuple

from forex_monitor.continuity.models import BackupArtifact, BackupState, BackupType, text, utc
from forex_monitor.errors import NotFoundError, StorageConflictError, ValidationError


class BackupCatalog:
    def __init__(self) -> None:
        self._backups: Dict[str, BackupArtifact] = {}
        self._resource_backups: Dict[str, list[str]] = {}
        self._lock = RLock()

    def register(self, backup: BackupArtifact) -> BackupArtifact:
        with self._lock:
            existing = self._backups.get(backup.backup_id)
            if existing is not None:
                if existing == backup:
                    return existing
                raise StorageConflictError("backup id already exists")
            if backup.base_backup_id:
                base = self._backups.get(backup.base_backup_id)
                if base is None or base.resource_id != backup.resource_id or base.state != BackupState.AVAILABLE:
                    raise ValidationError("backup base is unavailable or belongs to another resource")
                if backup.started_at < (base.completed_at or base.started_at):
                    raise ValidationError("incremental backup precedes its base")
                if backup.sequence_start is not None and base.sequence_end is not None and backup.sequence_start != base.sequence_end + 1:
                    raise ValidationError("incremental backup sequence does not continue base")
            self._backups[backup.backup_id] = backup
            self._resource_backups.setdefault(backup.resource_id, []).append(backup.backup_id)
            return backup

    def mark_corrupt(self, backup_id: str) -> BackupArtifact:
        with self._lock:
            current = self.require(backup_id)
            if current.state in {BackupState.EXPIRED, BackupState.DELETED}:
                raise ValidationError("expired or deleted backup cannot become corrupt")
            changed = replace(current, state=BackupState.CORRUPT)
            self._backups[current.backup_id] = changed
            return changed

    def expire(self, at: datetime) -> Tuple[BackupArtifact, ...]:
        instant = utc(at, "expiry time")
        changed = []
        with self._lock:
            for backup_id, current in list(self._backups.items()):
                if current.state == BackupState.AVAILABLE and current.expires_at is not None and current.expires_at <= instant:
                    if any(item.base_backup_id == backup_id and item.state == BackupState.AVAILABLE for item in self._backups.values()):
                        continue
                    value = replace(current, state=BackupState.EXPIRED)
                    self._backups[backup_id] = value
                    changed.append(value)
        return tuple(sorted(changed, key=lambda item: item.expires_at))

    def restore_chain(self, resource_id: str, target_time: datetime) -> Tuple[BackupArtifact, ...]:
        target = utc(target_time, "target time")
        with self._lock:
            available = [self._backups[item] for item in self._resource_backups.get(resource_id, ()) if self._backups[item].state == BackupState.AVAILABLE and (self._backups[item].completed_at or self._backups[item].started_at) <= target]
            full = [item for item in available if item.backup_type in {BackupType.FULL, BackupType.SNAPSHOT}]
            if not full:
                raise NotFoundError("no full backup is available before target time")
            base = max(full, key=lambda item: (item.completed_at or item.started_at, item.backup_id))
            chain = [base]
            current = base
            while True:
                children = [item for item in available if item.base_backup_id == current.backup_id]
                if not children:
                    break
                current = max(children, key=lambda item: (item.completed_at or item.started_at, item.backup_id))
                chain.append(current)
            return tuple(chain)

    def latest(self, resource_id: str) -> Optional[BackupArtifact]:
        with self._lock:
            values = [self._backups[item] for item in self._resource_backups.get(resource_id, ()) if self._backups[item].state == BackupState.AVAILABLE]
            return max(values, key=lambda item: (item.completed_at or item.started_at, item.backup_id)) if values else None

    def require(self, backup_id: str) -> BackupArtifact:
        value = self._backups.get(text(backup_id, "backup id"))
        if value is None:
            raise NotFoundError("backup does not exist")
        return value
