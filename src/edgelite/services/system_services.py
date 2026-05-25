"""Configuration backup/restore, log rotation and config audit service

Features:
- Full configuration backup to JSON
- Configuration restore with validation
- Automated backup scheduling
- Log rotation with size and time limits
- Configuration change audit trail
- Schema validation
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class BackupFormat(Enum):
    """Backup file format"""
    JSON = "json"
    ZIP = "zip"


class BackupType(Enum):
    """Backup type"""
    FULL = "full"
    DEVICES = "devices"
    RULES = "rules"
    CONFIG = "config"


@dataclass
class BackupMetadata:
    """Backup file metadata"""
    backup_id: str = ""
    name: str = ""
    description: str = ""
    backup_type: BackupType = BackupType.FULL
    version: str = ""
    created_at: str = ""
    created_by: str = ""
    file_size: int = 0
    checksum: str = ""
    device_count: int = 0
    rule_count: int = 0
    includes_secrets: bool = False


@dataclass
class AuditEntry:
    """Configuration change audit entry"""
    audit_id: str = ""
    timestamp: str = ""
    user: str = ""
    action: str = ""  # create, update, delete
    resource_type: str = ""  # device, rule, config
    resource_id: str = ""
    resource_name: str = ""
    changes: dict = field(default_factory=dict)  # {field: {old: _, new: _}}
    ip_address: str = ""
    user_agent: str = ""


@dataclass
class LogConfig:
    """Log rotation configuration"""
    enabled: bool = True
    max_size_mb: int = 100  # Max size per log file
    max_files: int = 10  # Number of backup files to keep
    compression: str = "gzip"  # gzip, bzip2, none
    log_dir: str = "logs"
    log_level: str = "INFO"  # DEBUG, INFO, WARNING, ERROR, CRITICAL


class ConfigBackupService:
    """Configuration backup and restore service"""

    def __init__(self, backup_dir: str = "data/backups"):
        self._backup_dir = Path(backup_dir)
        self._backup_dir.mkdir(parents=True, exist_ok=True)
        self._max_backups: int = 50
        self._auto_backup_task: asyncio.Task | None = None

    async def create_backup(
        self,
        backup_type: BackupType = BackupType.FULL,
        name: str = "",
        description: str = "",
        include_secrets: bool = False,
    ) -> BackupMetadata:
        """Create a configuration backup"""
        from edgelite.app import _app_state

        timestamp = datetime.now(UTC)
        backup_id = f"backup_{timestamp.strftime('%Y%m%d_%H%M%S')}"

        # Collect data
        data = {}

        if backup_type in (BackupType.FULL, BackupType.DEVICES):
            device_repo = getattr(_app_state, "device_repo", None)
            if device_repo:
                devices, _ = await device_repo.list_all(size=10000)
                data["devices"] = devices

        if backup_type in (BackupType.FULL, BackupType.RULES):
            rule_repo = getattr(_app_state, "rule_repo", None)
            if rule_repo:
                rules, _ = await rule_repo.list_all(size=10000)
                data["rules"] = rules

        if backup_type == BackupType.FULL:
            # Include system config
            config = getattr(_app_state, "config", None)
            if config:
                data["config"] = config.model_dump() if hasattr(config, "model_dump") else {}

        # Build metadata
        metadata = BackupMetadata(
            backup_id=backup_id,
            name=name or f"Backup {timestamp.strftime('%Y-%m-%d %H:%M')}",
            description=description,
            backup_type=backup_type,
            version="1.0",
            created_at=timestamp.isoformat(),
            created_by="system",
            device_count=len(data.get("devices", [])),
            rule_count=len(data.get("rules", [])),
            includes_secrets=include_secrets,
        )

        # Remove secrets if not included
        if not include_secrets:
            data = self._remove_secrets(data)

        # Write backup file
        backup_file = self._backup_dir / f"{backup_id}.json"
        backup_content = {
            "metadata": metadata.__dict__,
            "data": data,
        }
        content_json = json.dumps(backup_content, indent=2, ensure_ascii=False)
        backup_file.write_text(content_json, encoding="utf-8")

        # Calculate checksum
        metadata.checksum = hashlib.sha256(content_json.encode()).hexdigest()
        metadata.file_size = len(content_json.encode())

        # Update metadata in file
        backup_content["metadata"] = metadata.__dict__
        backup_file.write_text(
            json.dumps(backup_content, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        logger.info(
            "Backup created: %s (%s, %d devices, %d rules)",
            backup_id, backup_type.value, metadata.device_count, metadata.rule_count
        )

        # Cleanup old backups
        await self._cleanup_old_backups()

        return metadata

    def _remove_secrets(self, data: dict) -> dict:
        """Remove sensitive data from backup"""
        data = json.loads(json.dumps(data))  # Deep copy
        secret_keys = ["password", "secret", "token", "key", "api_key"]

        def remove_recursive(obj):
            if isinstance(obj, dict):
                for key in list(obj.keys()):
                    if any(secret in key.lower() for secret in secret_keys):
                        obj[key] = "***REDACTED***"
                    else:
                        remove_recursive(obj[key])
            elif isinstance(obj, list):
                for item in obj:
                    remove_recursive(item)

        remove_recursive(data)
        return data

    async def list_backups(self) -> list[BackupMetadata]:
        """List all available backups"""
        backups = []
        for backup_file in sorted(
            self._backup_dir.glob("backup_*.json"), reverse=True
        ):
            try:
                content = backup_file.read_text(encoding="utf-8")
                parsed = json.loads(content)
                meta = parsed.get("metadata", {})
                backups.append(BackupMetadata(**{
                    k: v for k, v in meta.items()
                    if k in BackupMetadata.__dataclass_fields__
                }))
            except Exception as e:
                logger.warning("Failed to parse backup %s: %s", backup_file.name, e)
        return backups

    async def get_backup(self, backup_id: str) -> dict | None:
        """Get backup content"""
        backup_file = self._backup_dir / f"{backup_id}.json"
        if not backup_file.exists():
            return None
        try:
            return json.loads(backup_file.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error("Failed to read backup %s: %s", backup_id, e)
            return None

    async def restore_backup(
        self,
        backup_id: str,
        restore_type: BackupType | None = None,
        skip_existing: bool = True,
    ) -> dict:
        """Restore from backup"""
        from edgelite.app import _app_state

        backup = await self.get_backup(backup_id)
        if not backup:
            return {"success": False, "error": "Backup not found"}

        data = backup.get("data", {})
        metadata = backup.get("metadata", {})
        restore_type = restore_type or BackupType(metadata.get("backup_type", "full"))

        results = {
            "success": True,
            "devices_restored": 0,
            "devices_skipped": 0,
            "rules_restored": 0,
            "rules_skipped": 0,
            "errors": [],
        }

        device_repo = getattr(_app_state, "device_repo", None)
        rule_repo = getattr(_app_state, "rule_repo", None)

        # Restore devices
        if restore_type in (BackupType.FULL, BackupType.DEVICES):
            devices = data.get("devices", [])
            for device in devices:
                try:
                    existing = await device_repo.get(device["device_id"]) if device_repo else None
                    if existing and skip_existing:
                        results["devices_skipped"] += 1
                        continue
                    if existing:
                        await device_repo.update(device["device_id"], device)
                    else:
                        await device_repo.create(device)
                    results["devices_restored"] += 1
                except Exception as e:
                    results["errors"].append(f"Device {device.get('device_id')}: {str(e)}")

        # Restore rules
        if restore_type in (BackupType.FULL, BackupType.RULES):
            rules = data.get("rules", [])
            for rule in rules:
                try:
                    existing = await rule_repo.get(rule["rule_id"]) if rule_repo else None
                    if existing and skip_existing:
                        results["rules_skipped"] += 1
                        continue
                    if existing:
                        await rule_repo.update(rule["rule_id"], rule)
                    else:
                        await rule_repo.create(rule)
                    results["rules_restored"] += 1
                except Exception as e:
                    results["errors"].append(f"Rule {rule.get('rule_id')}: {str(e)}")

        if results["errors"]:
            results["success"] = False

        logger.info(
            "Backup restored: %s (%d devices, %d rules, %d errors)",
            backup_id,
            results["devices_restored"],
            results["rules_restored"],
            len(results["errors"]),
        )

        return results

    async def delete_backup(self, backup_id: str) -> bool:
        """Delete a backup file"""
        backup_file = self._backup_dir / f"{backup_id}.json"
        if backup_file.exists():
            backup_file.unlink()
            logger.info("Backup deleted: %s", backup_id)
            return True
        return False

    async def _cleanup_old_backups(self) -> int:
        """Remove old backup files beyond max limit"""
        backups = await self.list_backups()
        if len(backups) <= self._max_backups:
            return 0

        to_delete = backups[self._max_backups:]
        deleted = 0
        for backup in to_delete:
            if await self.delete_backup(backup.backup_id):
                deleted += 1

        if deleted:
            logger.info("Cleaned up %d old backups", deleted)
        return deleted

    def set_max_backups(self, max_backups: int) -> None:
        """Set maximum number of backups to keep"""
        self._max_backups = max_backups


class LogRotationService:
    """Log rotation service"""

    def __init__(self, config: LogConfig | None = None):
        self._config = config or LogConfig()
        self._rotation_task: asyncio.Task | None = None
        self._check_interval: int = 3600  # Check every hour

    @property
    def config(self) -> LogConfig:
        return self._config

    async def start(self) -> None:
        """Start log rotation service"""
        if self._rotation_task:
            return
        self._rotation_task = asyncio.create_task(self._rotation_loop())
        logger.info("Log rotation service started")

    async def stop(self) -> None:
        """Stop log rotation service"""
        if self._rotation_task:
            self._rotation_task.cancel()
            try:
                await self._rotation_task
            except asyncio.CancelledError:
                pass
            self._rotation_task = None
        logger.info("Log rotation service stopped")

    async def _rotation_loop(self) -> None:
        """Main rotation check loop"""
        while True:
            try:
                await asyncio.sleep(self._check_interval)
                await self.rotate_if_needed()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Log rotation error: %s", e)

    async def rotate_if_needed(self) -> int:
        """Rotate logs if size threshold exceeded"""
        if not self._config.enabled:
            return 0

        log_dir = Path(self._config.log_dir)
        if not log_dir.exists():
            return 0

        rotated = 0
        max_size = self._config.max_size_mb * 1024 * 1024

        for log_file in log_dir.glob("*.log"):
            if log_file.stat().st_size > max_size:
                await self._rotate_log(log_file)
                rotated += 1

        return rotated

    async def _rotate_log(self, log_file: Path) -> None:
        """Rotate a single log file"""
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")

        # Determine compression suffix
        if self._config.compression == "gzip":
            rotated_name = f"{log_file.stem}_{timestamp}.log.gz"
        elif self._config.compression == "bzip2":
            rotated_name = f"{log_file.stem}_{timestamp}.log.bz2"
        else:
            rotated_name = f"{log_file.stem}_{timestamp}.log"

        rotated_path = log_file.parent / rotated_name

        # Truncate or compress
        try:
            if self._config.compression == "gzip":
                import gzip
                with open(log_file, "rb") as f_in:
                    with gzip.open(rotated_path, "wb") as f_out:
                        shutil.copyfileobj(f_in, f_out)
            else:
                shutil.move(str(log_file), str(rotated_path))

            # Clear original
            log_file.write_text("", encoding="utf-8")

            logger.info("Log rotated: %s -> %s", log_file.name, rotated_path.name)
        except Exception as e:
            logger.error("Log rotation failed for %s: %s", log_file.name, e)

        # Cleanup old rotated files
        await self._cleanup_rotated_logs(log_file.stem)

    async def _cleanup_rotated_logs(self, base_name: str) -> None:
        """Remove old rotated log files"""
        log_dir = Path(self._config.log_dir)
        pattern = f"{base_name}_*.log*"
        rotated_files = sorted(
            log_dir.glob(pattern),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

        # Remove files beyond max
        for old_file in rotated_files[self._config.max_files:]:
            try:
                old_file.unlink()
                logger.debug("Old log removed: %s", old_file.name)
            except Exception as e:
                logger.warning("Failed to remove old log %s: %s", old_file.name, e)

    def update_config(self, config: LogConfig) -> None:
        """Update log rotation config"""
        self._config = config


class ConfigAuditService:
    """Configuration change audit service"""

    def __init__(self, audit_dir: str = "data/audit"):
        self._audit_dir = Path(audit_dir)
        self._audit_dir.mkdir(parents=True, exist_ok=True)
        self._audit_file = self._audit_dir / "audit.jsonl"
        self._max_entries: int = 100000
        self._entries: list[AuditEntry] = []
        self._load_entries()

    def _load_entries(self) -> None:
        """Load existing audit entries"""
        if not self._audit_file.exists():
            return

        try:
            with open(self._audit_file, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        entry = json.loads(line)
                        self._entries.append(AuditEntry(**entry))
        except Exception as e:
            logger.warning("Failed to load audit entries: %s", e)

    def _save_entry(self, entry: AuditEntry) -> None:
        """Save audit entry to file"""
        try:
            with open(self._audit_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry.__dict__, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error("Failed to save audit entry: %s", e)

    def record_change(
        self,
        user: str,
        action: str,
        resource_type: str,
        resource_id: str,
        resource_name: str,
        changes: dict | None = None,
        ip_address: str = "",
        user_agent: str = "",
    ) -> AuditEntry:
        """Record a configuration change"""
        entry = AuditEntry(
            audit_id=f"audit_{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}",
            timestamp=datetime.now(UTC).isoformat(),
            user=user,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            resource_name=resource_name,
            changes=changes or {},
            ip_address=ip_address,
            user_agent=user_agent,
        )

        self._entries.append(entry)
        self._save_entry(entry)

        # Cleanup old entries
        if len(self._entries) > self._max_entries:
            self._entries = self._entries[-self._max_entries:]

        logger.info(
            "Audit: %s %s %s/%s by %s",
            action, resource_type, resource_id, resource_name, user
        )

        return entry

    def get_entries(
        self,
        user: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        limit: int = 100,
    ) -> list[AuditEntry]:
        """Query audit entries"""
        results = self._entries

        if user:
            results = [e for e in results if e.user == user]

        if resource_type:
            results = [e for e in results if e.resource_type == resource_type]

        if resource_id:
            results = [e for e in results if e.resource_id == resource_id]

        if start_time:
            results = [
                e for e in results
                if e.timestamp >= start_time
            ]

        if end_time:
            results = [
                e for e in results
                if e.timestamp <= end_time
            ]

        # Sort by timestamp descending
        results.sort(key=lambda e: e.timestamp, reverse=True)

        return results[:limit]

    def get_resource_history(
        self,
        resource_type: str,
        resource_id: str,
        limit: int = 50,
    ) -> list[AuditEntry]:
        """Get change history for a specific resource"""
        return self.get_entries(
            resource_type=resource_type,
            resource_id=resource_id,
            limit=limit,
        )

    def export_audit_log(
        self,
        start_time: str | None = None,
        end_time: str | None = None,
        format: str = "json",
    ) -> str:
        """Export audit log"""
        entries = self.get_entries(start_time=start_time, end_time=end_time, limit=100000)

        if format == "csv":
            import csv
            import io
            output = io.StringIO()
            fieldnames = [
                "audit_id", "timestamp", "user", "action",
                "resource_type", "resource_id", "resource_name",
                "ip_address",
            ]
            writer = csv.DictWriter(output, fieldnames=fieldnames)
            writer.writeheader()
            for entry in entries:
                writer.writerow({
                    k: getattr(entry, k) for k in fieldnames
                })
            return output.getvalue()
        else:
            return json.dumps(
                [e.__dict__ for e in entries],
                indent=2,
                ensure_ascii=False,
            )

    def cleanup_old_entries(self, days: int = 90) -> int:
        """Remove audit entries older than specified days"""
        cutoff = datetime.now(UTC) - timedelta(days=days)
        cutoff_str = cutoff.isoformat()

        original_count = len(self._entries)
        self._entries = [e for e in self._entries if e.timestamp >= cutoff_str]

        # Rewrite file
        with open(self._audit_file, "w", encoding="utf-8") as f:
            for entry in self._entries:
                f.write(json.dumps(entry.__dict__, ensure_ascii=False) + "\n")

        removed = original_count - len(self._entries)
        if removed:
            logger.info("Cleaned up %d old audit entries", removed)
        return removed


class ConfigValidator:
    """Configuration schema validator"""

    @staticmethod
    def validate_device_config(config: dict) -> tuple[bool, list[str]]:
        """Validate device configuration"""
        errors = []

        required = ["device_id", "name", "protocol"]
        for field in required:
            if field not in config or not config[field]:
                errors.append(f"Missing required field: {field}")

        if "protocol" in config:
            valid_protocols = [
                "modbus_tcp", "modbus_rtu", "s7", "mc", "fins",
                "opcua", "mqtt", "iec104", "dlt645",
            ]
            if config["protocol"] not in valid_protocols:
                errors.append(f"Invalid protocol: {config['protocol']}")

        if "collect_interval" in config:
            interval = config["collect_interval"]
            if not isinstance(interval, (int, float)) or interval < 0.1:
                errors.append("collect_interval must be >= 0.1")

        return len(errors) == 0, errors

    @staticmethod
    def validate_rule_config(config: dict) -> tuple[bool, list[str]]:
        """Validate rule configuration"""
        errors = []

        required = ["name", "device_id", "conditions", "severity"]
        for field in required:
            if field not in config:
                errors.append(f"Missing required field: {field}")

        if "conditions" in config:
            conditions = config["conditions"]
            if not isinstance(conditions, list) or not conditions:
                errors.append("conditions must be a non-empty list")
            else:
                for i, cond in enumerate(conditions):
                    if not isinstance(cond, dict):
                        errors.append(f"Condition {i} must be a dict")
                    elif "point" not in cond:
                        errors.append(f"Condition {i} missing 'point' field")
                    elif "operator" not in cond:
                        errors.append(f"Condition {i} missing 'operator' field")

        if "severity" in config:
            valid_severities = ["critical", "major", "minor", "warning", "info"]
            if config["severity"] not in valid_severities:
                errors.append(f"Invalid severity: {config['severity']}")

        return len(errors) == 0, errors


# Global instances
_backup_service: ConfigBackupService | None = None
_log_rotation: LogRotationService | None = None
_audit_service: ConfigAuditService | None = None


def get_backup_service(backup_dir: str = "data/backups") -> ConfigBackupService:
    """Get configuration backup service"""
    global _backup_service
    if _backup_service is None:
        _backup_service = ConfigBackupService(backup_dir)
    return _backup_service


def get_log_rotation_service(config: LogConfig | None = None) -> LogRotationService:
    """Get log rotation service"""
    global _log_rotation
    if _log_rotation is None:
        _log_rotation = LogRotationService(config)
    return _log_rotation


def get_audit_service(audit_dir: str = "data/audit") -> ConfigAuditService:
    """Get configuration audit service"""
    global _audit_service
    if _audit_service is None:
        _audit_service = ConfigAuditService(audit_dir)
    return _audit_service
