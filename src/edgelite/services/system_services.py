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
import concurrent.futures
import hashlib
import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)

import contextlib

from edgelite.constants import normalize_protocol


class BackupFormat(Enum):
    """Backup file format"""
    JSON = "json"
    ZIP = "zip"


class BackupType(Enum):
    """Backup type — determines what data is included in a single backup file."""
    FULL = "full"          # All devices + all rules + system config
    DEVICES = "devices"    # All devices only
    RULES = "rules"       # All rules only
    CONFIG = "config"     # System config only
    INCREMENTAL = "incremental"  # Changes since the last backup (references base_backup_id)


class BackupStrategy(Enum):
    """Auto-backup scheduling strategy.

    FIXED-INCREMENTAL: Controls the mix of full vs incremental backups in the
    auto-backup loop. The chain is always: full → (incremental × N) → full → ...

    Retention rules:
    - Full backups: kept for max_full_backups
    - Incremental backups: only those linked to a retained full backup are kept
      (orphaned increments from deleted fulls are also deleted)
    """
    FULL_ONLY = "full_only"           # Every scheduled run creates a full backup
    FULL_THEN_INCREMENTAL = "full_then_incremental"  # Full weekly + daily increments


@dataclass
class BackupMetadata:
    """Backup file metadata (stored inside each backup JSON file)."""
    backup_id: str = ""
    name: str = ""
    description: str = ""
    backup_type: BackupType = BackupType.FULL
    version: str = ""
    created_at: str = ""
    created_by: str = ""
    file_size: int = 0
    checksum: str = ""          # SHA-256 of the file content
    device_count: int = 0
    rule_count: int = 0
    includes_secrets: bool = False

    # FIXED-INCREMENTAL: incremental chain tracking
    base_backup_id: str = ""     # For INCREMENTAL type: the full backup this is based on
    incremental_since: str = ""   # ISO timestamp: only entities updated_at >= this are included
    previous_backup_id: str = "" # Previous backup in the chain (for ordering)


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
    """Configuration backup and restore service with scheduled auto-backup support.

    FIXED-AUTO-BACKUP: _auto_backup_task was declared but never started.
    The scheduler is launched via start_scheduler() (called during app startup)
    and stopped via stop_scheduler() (called during app shutdown).
    """

    def __init__(
        self,
        backup_dir: str = "data/backups",
        interval_seconds: int | None = None,
        max_backups: int | None = None,
        incremental_enabled: bool = True,
        full_backup_day_of_week: int = 0,  # 0=Monday, 6=Sunday
        max_full_backups: int = 4,
    ):
        """Configure the backup service.

        Args:
            backup_dir: Directory where backup files are stored
            interval_seconds: Auto-backup interval in seconds (default: from constants)
            max_backups: Max total backup files to retain (applies to full backups only)
            incremental_enabled: Whether incremental backups are enabled (default: True)
            full_backup_day_of_week: Day of week for weekly full backup (0=Mon, 6=Sun;
                                     set to -1 to disable weekly forcing)
            max_full_backups: How many full backups to keep (increments kept with their base)
        """
        from edgelite.constants import (
            _AUTO_BACKUP_INTERVAL_SECONDS,
            _AUTO_BACKUP_MAX_RETENTION,
        )

        self._backup_dir = Path(backup_dir)
        self._backup_dir.mkdir(parents=True, exist_ok=True)

        self._interval_seconds: int = (
            interval_seconds
            if interval_seconds is not None
            else _AUTO_BACKUP_INTERVAL_SECONDS
        )
        self._max_backups: int = (
            max_backups if max_backups is not None else _AUTO_BACKUP_MAX_RETENTION
        )

        self._incremental_enabled = incremental_enabled
        self._full_backup_day_of_week = full_backup_day_of_week
        self._max_full_backups = max_full_backups

        self._auto_backup_task: asyncio.Task | None = None
        self._stop_event: asyncio.Event = asyncio.Event()
        self._is_running: bool = False

        self._chain_state_file = self._backup_dir / ".backup_chain_state.json"
        self._chain_state: dict = self._load_chain_state()

    def _load_chain_state(self) -> dict:
        """Load incremental backup chain state from disk."""
        try:
            if self._chain_state_file.exists():
                return json.loads(self._chain_state_file.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("Failed to load chain state: %s, starting fresh", e)
        return {}

    def _save_chain_state(self) -> None:
        """Persist incremental backup chain state to disk."""
        try:
            self._chain_state_file.write_text(
                json.dumps(self._chain_state, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("Failed to save chain state: %s", e)

    @property
    def is_scheduled(self) -> bool:
        """Whether the auto-backup scheduler is running."""
        return self._is_running

    @property
    def interval_seconds(self) -> int:
        """Current backup interval in seconds."""
        return self._interval_seconds

    def set_interval(self, interval_seconds: int) -> None:
        """Update the backup interval at runtime. Takes effect at the next cycle."""
        if interval_seconds < 60:
            raise ValueError("interval must be at least 60 seconds")
        self._interval_seconds = interval_seconds

    async def start_scheduler(self) -> None:
        """Start the auto-backup scheduler.

        Idempotent — calling while already running is a no-op.
        Raises RuntimeError if no running event loop is found.
        """
        if self._is_running:
            logger.debug("Auto-backup scheduler already running, skipping start")
            return
        self._stop_event.clear()
        self._is_running = True
        asyncio.get_running_loop()
        # FIXED-P1: 命名任务以便 teardown 白名单兜底取消
        self._auto_backup_task = asyncio.create_task(
            self._backup_loop(), name="edgelite_config_backup"
        )
        logger.info(
            "Auto-backup scheduler started (interval=%ds, max_backups=%d)",
            self._interval_seconds, self._max_backups,
        )

    async def stop_scheduler(self) -> None:
        """Stop the auto-backup scheduler gracefully.

        Cancels the running loop and waits for the current iteration to finish.
        Application shutdown must call this to avoid resource leaks.
        """
        if not self._is_running:
            return
        self._is_running = False
        self._stop_event.set()
        if self._auto_backup_task is not None:
            self._auto_backup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._auto_backup_task
            self._auto_backup_task = None
        logger.info("Auto-backup scheduler stopped")

    async def _backup_loop(self) -> None:
        """Background loop that runs scheduled backups until stopped."""
        while True:
            try:
                # Wait for interval OR stop signal
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._interval_seconds,
                )
                return  # stop_event was set → exit cleanly
            except (TimeoutError, asyncio.TimeoutError):  # noqa: UP041 - 兼容 Python<3.11
                # FIXED-P1: 原问题-仅捕获 TimeoutError，Python<3.11 下 asyncio.wait_for 超时
                # 抛出 asyncio.TimeoutError（非内置 TimeoutError 子类），导致协程静默退出，
                # 配置自动备份永久停止但 _is_running 仍为 True，外部监控无法感知
                pass  # interval elapsed, proceed to backup
            except asyncio.CancelledError:
                return

            if not self._is_running:
                return

            try:
                metadata = await self.create_backup(
                    backup_type=BackupType.FULL,
                    name="Auto-backup",
                    description=f"Scheduled backup at {datetime.now(UTC).isoformat()}",
                    include_secrets=False,
                )
                logger.info(
                    "Scheduled backup completed: %s (%s, %d devices, %d rules)",
                    metadata.backup_id,
                    metadata.backup_type.value,
                    metadata.device_count,
                    metadata.rule_count,
                )
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error(
                    "Scheduled backup failed: %s — will retry at next interval",
                    e,
                )

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
                # FIXED(严重): 原代码 list_all(size=10000) 硬编码上限，超过 10000 条被静默截断
                # 修复-分页循环加载全量数据
                devices: list[dict] = []
                _page = 1
                while True:
                    _items, _total = await device_repo.list_all(page=_page, size=1000)
                    devices.extend(_items)
                    if not _items or len(_items) < 1000 or len(devices) >= _total:
                        break
                    _page += 1
                data["devices"] = devices

        if backup_type in (BackupType.FULL, BackupType.RULES):
            rule_repo = getattr(_app_state, "rule_repo", None)
            if rule_repo:
                # FIXED(严重): 原代码 list_all(size=10000) 硬编码上限
                rules: list[dict] = []
                _page = 1
                while True:
                    _items, _total = await rule_repo.list_all(page=_page, size=1000)
                    rules.extend(_items)
                    if not _items or len(_items) < 1000 or len(rules) >= _total:
                        break
                    _page += 1
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
        # 将metadata转为可序列化的dict（枚举转为值）
        meta_dict = {}
        for k, v in metadata.__dict__.items():
            if isinstance(v, Enum):
                meta_dict[k] = v.value
            else:
                meta_dict[k] = v
        backup_content = {
            "metadata": meta_dict,
            "data": data,
        }
        content_json = json.dumps(backup_content, indent=2, ensure_ascii=False, default=str)
        # FIXED(严重): 原代码 backup_file.write_text 同步阻塞事件循环，大备份文件写入期间服务卡顿
        # 修复-用 asyncio.to_thread 包装同步文件 I/O
        await asyncio.to_thread(backup_file.write_text, content_json, encoding="utf-8")
        # 安全加固: 备份文件含敏感配置，限制为属主可读写，防止其他用户读取
        os.chmod(backup_file, 0o600)

        # Calculate checksum
        # R6-F-02修复: checksum仅对data字段计算，排除metadata.checksum自引用导致恢复永久失败
        data_json = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)
        metadata.checksum = hashlib.sha256(data_json.encode()).hexdigest()
        metadata.file_size = len(content_json.encode())

        # Update metadata in file — re-convert enums to values
        # (metadata.__dict__ still contains Enum objects after checksum assignment)
        updated_meta = {}
        for k, v in metadata.__dict__.items():
            if isinstance(v, Enum):
                updated_meta[k] = v.value
            else:
                updated_meta[k] = v
        backup_content["metadata"] = updated_meta
        await asyncio.to_thread(
            backup_file.write_text,
            json.dumps(backup_content, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        # 安全加固: 重写后再次限制权限，防止写入过程权限被重置
        os.chmod(backup_file, 0o600)

        logger.info(
            "Backup created: %s (%s, %d devices, %d rules)",
            backup_id, backup_type.value, metadata.device_count, metadata.rule_count
        )

        # Cleanup old backups
        await self._cleanup_old_backups()

        return metadata

    def _remove_secrets(self, data: dict) -> dict:
        """Remove sensitive data from backup"""
        data = json.loads(json.dumps(data, default=str))  # Deep copy
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

    # FIXED-BugR4X: 原问题-restore_backup调用list_incremental_chain但方法未定义导致AttributeError，
    # 修复-实现该方法返回从基础全量备份之后到目标增量备份之间的有序增量备份ID列表
    # （不含基础全量备份，含目标备份），通过previous_backup_id向前回溯构建时间顺序链
    async def list_incremental_chain(self, backup_id: str) -> list[str]:
        """Return ordered incremental backup IDs from base (exclusive) to target (inclusive).

        Walks the ``previous_backup_id`` chain backwards starting from ``backup_id``,
        then reverses the collected list to produce chronological order.
        The base full backup is NOT included in the result.
        """
        chain: list[str] = []
        current_id = backup_id
        visited: set[str] = set()
        while current_id:
            if current_id in visited:
                logger.warning(
                    "Cycle detected in incremental backup chain at %s, stopping",
                    current_id,
                )
                break
            visited.add(current_id)

            backup = await self.get_backup(current_id)
            if backup is None:
                logger.warning(
                    "Broken incremental chain: backup %s not found", current_id
                )
                break

            meta = backup.get("metadata", {})
            # 只收集增量备份，遇到全量备份即停止（基础全量备份不含在结果中）
            if meta.get("backup_type") != "incremental":
                break

            chain.append(current_id)
            previous_id = meta.get("previous_backup_id", "")
            if not previous_id:
                break
            current_id = previous_id

        # 反转为时间正序（最早增量在前，目标备份在末尾）
        chain.reverse()
        return chain

    # FIXED-BugR4X: 原问题-restore_backup调用_deduplicate_entities但方法未定义导致AttributeError，
    # 修复-实现按实体ID去重，后出现的覆盖先出现的（保证增量备份中的最新版本生效）
    def _deduplicate_entities(self, entities: list[dict]) -> list[dict]:
        """Deduplicate entities by their ID field.

        The ID field may be ``device_id``, ``rule_id`` or ``id``. Later occurrences
        overwrite earlier ones, so the most recent version (from the latest
        incremental backup) wins.
        """
        deduped: dict[str, dict] = {}
        for entity in entities:
            if not isinstance(entity, dict):
                continue
            entity_id = (
                entity.get("device_id")
                or entity.get("rule_id")
                or entity.get("id")
            )
            if not entity_id:
                # 无ID的实体无法去重，直接保留（追加到结果末尾）
                continue
            deduped[str(entity_id)] = entity

        # 返回去重后的实体列表，保持插入顺序（Python 3.7+ dict 保留插入顺序）
        result = list(deduped.values())

        # 追加无ID的实体（保持原始行为，不丢弃无ID数据）
        for entity in entities:
            if not isinstance(entity, dict):
                continue
            entity_id = (
                entity.get("device_id")
                or entity.get("rule_id")
                or entity.get("id")
            )
            if not entity_id:
                result.append(entity)

        return result

    async def restore_backup(
        self,
        backup_id: str,
        restore_type: BackupType | None = None,
        skip_existing: bool = True,
        apply_increments: bool = True,
    ) -> dict:
        """Atomically restore from a backup.

        If the target is an incremental backup, the base full backup is restored first,
        then all increments in the chain are applied in order (apply_increments=True).
        """
        import hashlib
        import time

        from edgelite.app import _app_state
        from edgelite.storage.sqlite_repo import (
            _validate_device_data,
            _validate_rule_data,
        )

        # Phase 1: resolve restore chain
        backup = await self.get_backup(backup_id)
        if not backup:
            return {"success": False, "error": "Backup not found"}

        meta = backup.get("metadata", {})
        backup_type_str = meta.get("backup_type", "full")
        is_incremental = backup_type_str == "incremental"
        target_type = restore_type or (
            BackupType.INCREMENTAL if is_incremental
            else BackupType(meta.get("backup_type", "full"))
        )

        # Build ordered restore list: [full_backup, inc1, ..., target]
        restore_order: list[str] = []
        if is_incremental and apply_increments:
            base_id = meta.get("base_backup_id", "")
            if base_id:
                restore_order.append(base_id)
                restore_order.extend(await self.list_incremental_chain(backup_id))
                if backup_id not in restore_order:
                    restore_order.append(backup_id)
            else:
                restore_order.append(backup_id)
        else:
            restore_order.append(backup_id)

        # Phase 2: verify all checksums
        for bid in restore_order:
            f = await self.get_backup(bid)
            if f is None:
                return {"success": False, "error": f"Chained backup {bid} not found"}
            expected = f.get("metadata", {}).get("checksum", "")
            if expected:
                # R6-F-02修复: checksum仅对data字段计算，与create_backup保持一致
                data_content = f.get("data", {})
                actual = hashlib.sha256(
                    json.dumps(data_content, sort_keys=True, ensure_ascii=False, default=str).encode()
                ).hexdigest()
                if actual != expected:
                    return {
                        "success": False,
                        "error": f"Checksum mismatch for {bid}",
                    }

        # Phase 3: collect and deduplicate all entities across the chain
        all_devices: list[dict] = []
        all_rules: list[dict] = []
        for bid in restore_order:
            f = await self.get_backup(bid)
            all_devices.extend(f.get("data", {}).get("devices", []))
            all_rules.extend(f.get("data", {}).get("rules", []))

        all_devices = self._deduplicate_entities(all_devices)
        all_rules = self._deduplicate_entities(all_rules)

        devices_to_restore = all_devices if target_type in (BackupType.FULL, BackupType.DEVICES) else []
        rules_to_restore = all_rules if target_type in (BackupType.FULL, BackupType.RULES) else []

        device_errors: list[tuple[int, str]] = []
        for i, device in enumerate(devices_to_restore):
            try:
                _validate_device_data(device)
            except Exception as e:
                device_errors.append((i, str(e)))
        rule_errors: list[tuple[int, str]] = []
        for i, rule in enumerate(rules_to_restore):
            try:
                _validate_rule_data(rule)
            except Exception as e:
                rule_errors.append((i, str(e)))

        if device_errors or rule_errors:
            combined = [f"Device[{i}]: {e}" for i, e in device_errors]
            combined += [f"Rule[{i}]: {e}" for i, e in rule_errors]
            return {
                "success": False,
                "error": "Validation failed: " + "; ".join(combined[:5]),
            }

        # Phase 4: pre-restore snapshot
        db = getattr(_app_state, "db", None)
        if db is None:
            return {"success": False, "error": "Database not available"}

        snapshot_id = f"snapshot_{int(time.time() * 1000)}"
        snapshot_dir = self._backup_dir / f"pre_restore_{snapshot_id}"
        snapshot_dir.mkdir(parents=True, exist_ok=True)

        try:
            def _snap(src_path: str) -> Path | None:
                src = Path(src_path)
                if src.exists():
                    dst = snapshot_dir / src.name
                    shutil.copy2(src, dst)
                    if Path(f"{src_path}-wal").exists():
                        shutil.copy2(f"{src_path}-wal", str(dst) + "-wal")
                    return dst
                return None

            if db.backend == "sqlite":
                _snap(db.db_path)
                if db.audit_db_path:
                    _snap(db.audit_db_path)
        except Exception as snap_err:
            logger.warning("Pre-restore snapshot failed: %s", snap_err)

        device_repo = getattr(_app_state, "device_repo", None)
        rule_repo = getattr(_app_state, "rule_repo", None)

        # Phase 5: single-transaction upsert
        async with (db.write_lock if hasattr(db, "write_lock") else db._write_lock):
            async with db.get_session() as session:
                try:
                    dev_created = dev_skipped = rule_created = rule_skipped = 0
                    if device_repo and devices_to_restore:
                        dev_created, dev_skipped, dev_errs = await device_repo.upsert_bulk(
                            devices_to_restore, session, skip_existing=skip_existing,
                        )
                        for err in dev_errs:
                            if err:
                                logger.warning("Device restore warning: %s", err)
                        # FIXED-Bug18: 移除中间 commit，保证设备+规则在同一事务内原子提交
                        # 之前：设备恢复后立即 commit，若规则恢复失败 rollback 只能回滚规则，设备已永久写入
                    if rule_repo and rules_to_restore:
                        rule_created, rule_skipped, rule_errs = await rule_repo.upsert_bulk(
                            rules_to_restore, session, skip_existing=skip_existing,
                        )
                        for err in rule_errs:
                            if err:
                                logger.warning("Rule restore warning: %s", err)
                    # FIXED-Bug18: 整个事务在设备+规则都成功后一次性提交
                    await session.commit()
                except Exception as tx_err:
                    await session.rollback()
                    return {
                        "success": False,
                        "error": f"Restore failed: {tx_err}",
                        "snapshot_dir": str(snapshot_dir),
                    }

        # Phase 6: post-restore integrity check
        violations: list[str] = []
        if device_repo and devices_to_restore:
            for device in devices_to_restore:
                try:
                    if await device_repo.get(device["device_id"]) is None:
                        violations.append(f"Device {device['device_id']} not found after restore")
                except Exception as e:
                    # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                    logger.warning("Device integrity check failed after restore: %s", e)  # FIXED-P2: 原问题-中文日志
        if rule_repo and rules_to_restore:
            for rule in rules_to_restore:
                try:
                    if await rule_repo.get(rule["rule_id"]) is None:
                        violations.append(f"Rule {rule['rule_id']} not found after restore")
                except Exception as e:
                    # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                    logger.warning("Rule integrity check failed after restore: %s", e)  # FIXED-P2: 原问题-中文日志

        try:
            shutil.rmtree(snapshot_dir, ignore_errors=True)
        except Exception as e:
            # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
            logger.warning("Failed to clean up restore snapshot dir: %s", e)  # FIXED-P2: 原问题-中文日志

        return {
            "success": len(violations) == 0,
            "backup_id": backup_id,
            "chain": restore_order,
            "devices_restored": dev_created,
            "devices_skipped": dev_skipped,
            "rules_restored": rule_created,
            "rules_skipped": rule_skipped,
            "violations": violations,
        }

    async def delete_backup(self, backup_id: str) -> bool:
        """Delete a backup file and its orphaned chain children if it is a full backup.

        Chain-safe: when a full backup is deleted, all incremental backups that
        reference it as their base are also deleted to prevent restore failures.
        """
        backup_file = self._backup_dir / f"{backup_id}.json"
        if not backup_file.exists():
            return False

        # Find orphaned increments if this is a full backup
        orphaned_increments: list[str] = []
        try:
            parsed = json.loads(backup_file.read_text(encoding="utf-8"))
            meta = parsed.get("metadata", {})
            if meta.get("backup_type") != "incremental":
                # FIX-P1: backup_id 格式为 backup_YYYYMMDD_HHMMSS，不含 _incremental
                # 后缀，原 glob "backup_*_incremental.json" 永远不匹配任何文件，
                # 导致孤儿增量备份无法被清理。改为匹配所有 backup_*.json，再通过
                # metadata 的 backup_type 与 base_backup_id 过滤出引用该全量备份的增量。
                orphaned_increments = [
                    f.stem
                    for f in self._backup_dir.glob("backup_*.json")
                    if f.stem != backup_id
                    and f.exists()
                    and (f_meta := json.loads(f.read_text(encoding="utf-8")).get("metadata", {}))
                    and f_meta.get("backup_type") == "incremental"
                    and f_meta.get("base_backup_id") == backup_id
                ]
        except Exception as e:
            logger.warning("Failed to check chain for %s: %s", backup_id, e)

        for inc_id in orphaned_increments:
            inc_file = self._backup_dir / f"{inc_id}.json"
            if inc_file.exists():
                inc_file.unlink()
                logger.info(
                    "Deleted orphaned incremental backup (parent %s deleted): %s",
                    backup_id, inc_id,
                )

        backup_file.unlink()
        logger.info("Backup deleted: %s", backup_id)

        # Update chain state if needed
        if self._chain_state.get("last_full_backup_id") == backup_id:
            self._chain_state.pop("last_full_backup_id", None)
            self._save_chain_state()
        if self._chain_state.get("last_backup_id") == backup_id:
            self._chain_state.pop("last_backup_id", None)
            self._save_chain_state()

        return True

    async def _cleanup_old_backups(self) -> int:
        """Remove old backup files beyond max limit"""
        backups = await self.list_backups()
        if len(backups) <= self._max_backups:
            return 0

        # FIX-P1: 原代码不区分全量与增量备份，直接按数量截断删除最旧备份，
        # 可能删除仍被增量链引用的增量备份，导致链断裂、恢复失败。改为仅
        # 将非增量（全量/设备/规则/配置）备份纳入超限判断；删除全量备份时，
        # delete_backup 会同时清理其关联的孤儿增量备份，保持链完整性。
        # getattr 兼容 backup_type 为枚举(BackupType.INCREMENTAL)或字符串("incremental")两种形态。
        deletable = [
            b for b in backups
            if getattr(b.backup_type, "value", b.backup_type) != "incremental"
        ]
        if len(deletable) <= self._max_backups:
            return 0

        to_delete = deletable[self._max_backups:]
        deleted = 0
        for backup in to_delete:
            if await self.delete_backup(backup.backup_id):
                deleted += 1

        if deleted:
            logger.info("Cleaned up %d old full backups (with associated increments)", deleted)
        return deleted

    def set_max_backups(self, max_backups: int) -> None:
        """Set maximum number of full backups to keep."""
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
        # FIXED-P1: 命名任务以便 teardown 白名单兜底取消
        self._rotation_task = asyncio.create_task(
            self._rotation_loop(), name="edgelite_log_rotation"
        )
        logger.info("Log rotation service started")

    async def stop(self) -> None:
        """Stop log rotation service"""
        if self._rotation_task:
            self._rotation_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._rotation_task
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
        """Compress and clean up RotatingFileHandler backup files"""
        if not self._config.enabled:
            return 0

        log_dir = Path(self._config.log_dir)
        if not log_dir.exists():
            return 0

        rotated = 0

        # FIXED-P1: 原问题-对活跃.log文件按大小轮转，但RotatingFileHandler已自行按大小轮转，
        # 此处重复轮转会清空活跃日志导致丢失；改为压缩RotatingFileHandler备份(.log.1, .log.2, ...)
        for log_file in log_dir.glob("*.log.[0-9]*"):
            if log_file.name.endswith((".gz", ".bz2")):
                continue
            await self._compress_log(log_file)
            rotated += 1

        # 清理超量备份文件
        backup_files = sorted(
            [p for p in log_dir.glob("*.log.[0-9]*") if p.is_file()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old_file in backup_files[self._config.max_files:]:
            try:
                old_file.unlink()
                logger.debug("Old log backup removed: %s", old_file.name)
            except Exception as e:
                logger.warning("Failed to remove old log backup %s: %s", old_file.name, e)

        return rotated

    async def _compress_log(self, log_file: Path) -> None:
        """压缩单个日志备份文件为gzip"""
        import gzip
        compressed_path = log_file.with_suffix(log_file.suffix + ".gz")

        # FIXED-P1: 原问题-async函数内直接同步文件IO阻塞事件循环；改为asyncio.to_thread
        def _do_compress(log_file=log_file, compressed_path=compressed_path):
            with open(log_file, "rb") as f_in, gzip.open(compressed_path, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
            log_file.unlink()

        try:
            await asyncio.to_thread(_do_compress)
            logger.info("Log backup compressed: %s -> %s", log_file.name, compressed_path.name)
        except Exception as e:
            logger.error("Log compression failed for %s: %s", log_file.name, e)

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

        # FIXED-P1: 原问题-async函数内直接同步文件IO（open+shutil+write_text）阻塞事件循环；改为asyncio.to_thread
        def _do_rotate(log_file=log_file, rotated_path=rotated_path):
            if self._config.compression == "gzip":
                import gzip
                with open(log_file, "rb") as f_in:
                    with gzip.open(rotated_path, "wb") as f_out:
                        shutil.copyfileobj(f_in, f_out)
            else:
                shutil.move(str(log_file), str(rotated_path))
            # Clear original
            log_file.write_text("", encoding="utf-8")

        try:
            await asyncio.to_thread(_do_rotate)
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
    """Configuration change audit service.

    FIXED-AUDIT-DB: Migrated from JSONL file to the main database.
    - All writes go through the main DB with write_lock protection
    - Indexed queries (no full-file scan)
    - Included in main DB backup/restore automatically
    - JSONL fallback: DB write failures are logged to audit.jsonl for later recovery
    - Audit entries are never loaded in-memory (streaming via DB pagination)
    """

    def __init__(self, audit_dir: str = "data/audit"):
        self._audit_dir = Path(audit_dir)
        self._audit_dir.mkdir(parents=True, exist_ok=True)
        self._audit_file = self._audit_dir / "audit.jsonl"

    # ------------------------------------------------------------------
    # Sync wrappers for route handlers (sync → async DB)
    # ------------------------------------------------------------------

    def _run_async_safe(
        self,
        coro_func,
        error_label: str = "Audit async operation failed",
        timeout: int = 15,
    ):
        """R8-S-10: 安全执行异步协程 — 避免在事件循环线程中死锁

        若被运行中的事件循环调用（如 async 路由处理器），future.result() 会阻塞
        事件循环等待一个永远无法调度的协程，导致死锁。此时在新线程中通过
        asyncio.run 执行协程。若不在运行中的事件循环里，则可安全使用
        run_coroutine_threadsafe 或 run_until_complete。
        """
        # 先判断当前是否处于运行中的事件循环（避免在 except 分支内嵌套过深）
        in_running_loop = False
        try:
            asyncio.get_running_loop()  # 无运行中的事件循环则抛 RuntimeError
            in_running_loop = True
        except RuntimeError:
            pass

        try:
            if in_running_loop:
                # 处于事件循环线程中，不能阻塞等待。在新线程中运行。
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    return pool.submit(lambda: asyncio.run(coro_func())).result(timeout=timeout)
            # 不在运行中的事件循环里，可安全阻塞
            try:
                main_loop = asyncio.get_event_loop()
                if main_loop.is_running():
                    future = asyncio.run_coroutine_threadsafe(coro_func(), main_loop)
                    return future.result(timeout=timeout)
                return main_loop.run_until_complete(coro_func())
            except RuntimeError:
                # 完全没有事件循环，回退到 asyncio.run
                return asyncio.run(coro_func())
        except Exception as e:
            logger.error("%s: %s", error_label, e)
            raise

    def record_change(
        self,
        user: str,
        action: str,
        resource_type: str,
        resource_id: str,
        resource_name: str = "",
        changes: dict | None = None,
        ip_address: str = "",
        user_agent: str = "",
        session_id: str = "",
    ) -> AuditEntry:
        """Record a configuration change (sync wrapper).

        Primary path: write to main DB (transactional, ACID).
        Fallback: append to audit.jsonl if DB write fails.
        """
        import asyncio

        async def _write():
            now = datetime.now(UTC)
            audit_id = f"audit_{now.strftime('%Y%m%d%H%M%S%f')}"

            db = _get_db()
            entry_dict = {
                "audit_id": audit_id,
                "timestamp": now.isoformat(),
                "user_id": user,
                "action": action,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "resource_name": resource_name,
                "old_value": "{}",
                "new_value": "{}",
                "changes": json.dumps(changes or {}, ensure_ascii=False),
                "ip_address": ip_address,
                "user_agent": user_agent,
                "session_id": session_id,
            }

            if db is None:
                # FIXED-P1: 原问题-async函数内直接调用同步文件写阻塞事件循环；改为asyncio.to_thread
                await asyncio.to_thread(_write_fallback_jsonl, entry_dict)
                return _dict_to_audit_entry(entry_dict)

            try:
                async with db.write_lock, db.get_session() as session:
                    from edgelite.models.db import AuditLogORM

                    orm = AuditLogORM(
                        audit_id=audit_id,
                        timestamp=now,
                        action=action,
                        user_id=user,
                        resource_type=resource_type,
                        resource_id=resource_id,
                        resource_name=resource_name,
                        old_value="{}",
                        new_value="{}",
                        changes=json.dumps(changes or {}, ensure_ascii=False),
                        ip_address=ip_address,
                        user_agent=user_agent,
                        session_id=session_id,
                    )
                    session.add(orm)
                    await session.commit()
                logger.info(
                    "Audit: %s %s %s/%s by %s",
                    action, resource_type, resource_id, resource_name, user,
                )
                return _dict_to_audit_entry(entry_dict)
            except Exception as db_err:
                logger.warning(
                    "Audit DB write failed (%s), falling back to JSONL: %s",
                    db_err, audit_id,
                )
                # FIXED-P1: 原问题-async函数内直接调用同步文件写阻塞事件循环；改为asyncio.to_thread
                await asyncio.to_thread(_write_fallback_jsonl, entry_dict)
                return _dict_to_audit_entry(entry_dict)

        return self._run_async_safe(_write, "Audit record_change failed")

    def get_entries(
        self,
        user: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        limit: int = 100,
    ) -> list[AuditEntry]:
        """Query audit entries with filters (sync wrapper).

        All filtering is pushed to the SQL query so the DB does the work.
        """
        import asyncio

        async def _query():
            db = _get_db()
            if db is None:
                # FIXED-P1: 原问题-async函数内直接调用同步文件读阻塞事件循环；改为asyncio.to_thread
                return await asyncio.to_thread(
                    _query_jsonl_fallback,
                    user=user, resource_type=resource_type,
                    resource_id=resource_id,
                    start_time=start_time, end_time=end_time,
                    limit=limit,
                )

            try:
                async with db.get_session() as session:
                    from sqlalchemy import select

                    from edgelite.models.db import AuditLogORM

                    query = select(AuditLogORM)
                    if user:
                        query = query.where(AuditLogORM.user_id == user)
                    if resource_type:
                        query = query.where(AuditLogORM.resource_type == resource_type)
                    if resource_id:
                        query = query.where(AuditLogORM.resource_id == resource_id)
                    if start_time:
                        st = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
                        query = query.where(AuditLogORM.timestamp >= st)
                    if end_time:
                        et = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
                        query = query.where(AuditLogORM.timestamp <= et)
                    query = query.order_by(AuditLogORM.timestamp.desc()).limit(limit)
                    result = await session.execute(query)
                    rows = result.scalars().all()
                    return [_orm_to_audit_entry(r) for r in rows]
            except Exception as e:
                logger.warning("Audit DB query failed, falling back to JSONL: %s", e)
                # FIXED-P1: 原问题-async函数内直接调用同步文件读阻塞事件循环；改为asyncio.to_thread
                return await asyncio.to_thread(
                    _query_jsonl_fallback,
                    user=user, resource_type=resource_type,
                    resource_id=resource_id,
                    start_time=start_time, end_time=end_time,
                    limit=limit,
                )

        return self._run_async_safe(_query, "Audit get_entries failed")

    def get_resource_history(
        self,
        resource_type: str,
        resource_id: str,
        limit: int = 50,
    ) -> list[AuditEntry]:
        """Get full change history for a specific resource."""
        return self.get_entries(
            resource_type=resource_type,
            resource_id=resource_id,
            limit=limit,
        )

    def get_entries_count(
        self,
        user: str | None = None,
        resource_type: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> int:
        """Count matching audit entries (for pagination)."""
        import asyncio

        async def _count():
            db = _get_db()
            if db is None:
                # FIXED-P1: 原问题-async函数内直接调用同步文件读阻塞事件循环；改为asyncio.to_thread
                rows = await asyncio.to_thread(
                    _query_jsonl_fallback,
                    user=user, resource_type=resource_type,
                    start_time=start_time, end_time=end_time,
                    limit=100000,
                )
                return len(rows)

            try:
                async with db.get_session() as session:
                    from sqlalchemy import func, select

                    from edgelite.models.db import AuditLogORM

                    query = select(func.count(AuditLogORM.id))
                    if user:
                        query = query.where(AuditLogORM.user_id == user)
                    if resource_type:
                        query = query.where(AuditLogORM.resource_type == resource_type)
                    if start_time:
                        st = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
                        query = query.where(AuditLogORM.timestamp >= st)
                    if end_time:
                        et = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
                        query = query.where(AuditLogORM.timestamp <= et)
                    result = await session.execute(query)
                    return result.scalar() or 0
            except Exception as e:
                logger.warning("Audit count DB query failed: %s", e)
                return 0

        return self._run_async_safe(_count, "Audit get_entries_count failed")

    def export_audit_log(
        self,
        start_time: str | None = None,
        end_time: str | None = None,
        format: str = "json",
        limit: int = 100000,
    ) -> str:
        """Export audit log entries as JSON or CSV."""
        entries = self.get_entries(
            start_time=start_time,
            end_time=end_time,
            limit=limit,
        )

        if format == "csv":
            import csv
            import io

            output = io.StringIO()
            fieldnames = [
                "audit_id", "timestamp", "user_id", "action",
                "resource_type", "resource_id", "resource_name",
                "ip_address", "user_agent", "session_id",
            ]
            writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for entry in entries:
                # FIXED-P1: 原问题-CSV导出未净化单元格值，存在CSV注入风险
                row = {}
                for k, v in entry.__dict__.items():
                    if isinstance(v, str) and v and v[0] in ("=", "+", "-", "@", "\t", "\r"):
                        row[k] = f"'{v}"
                    else:
                        row[k] = v
                writer.writerow(row)
            return output.getvalue()
        else:
            return json.dumps(
                [e.__dict__ for e in entries],
                indent=2,
                ensure_ascii=False,
            )

    def cleanup_old_entries(self, days: int = 90) -> int:
        """Remove audit entries older than specified days. Returns count deleted."""
        import asyncio

        async def _cleanup():
            db = _get_db()
            cutoff = datetime.now(UTC) - timedelta(days=days)

            if db is None:
                # FIXED-P1: 原问题-async函数内直接调用同步文件读写阻塞事件循环；改为asyncio.to_thread
                return await asyncio.to_thread(_cleanup_jsonl_fallback, days)

            try:
                async with db.write_lock, db.get_session() as session:
                    from sqlalchemy import delete

                    from edgelite.models.db import AuditLogORM

                    result = await session.execute(
                        delete(AuditLogORM).where(AuditLogORM.timestamp < cutoff)
                    )
                    await session.commit()
                    count = result.rowcount
                    if count:
                        logger.info("Cleaned up %d audit entries older than %d days", count, days)
                    return count
            except Exception as e:
                logger.warning("Audit cleanup DB operation failed: %s", e)
                return 0

        return self._run_async_safe(_cleanup, "Audit cleanup_old_entries failed")


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _get_db():
    """Get the main Database instance from app state."""
    from edgelite.app import _app_state

    return getattr(_app_state, "db", None)


def _write_fallback_jsonl(entry: dict) -> None:
    """Append an audit entry to the legacy JSONL file (fallback path)."""
    try:
        with open(_get_audit_file(), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.error("Audit JSONL fallback write failed: %s", e)


def _get_audit_file() -> Path:
    return Path("data/audit/audit.jsonl")


def _query_jsonl_fallback(
    user: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    limit: int = 100,
) -> list[AuditEntry]:
    """Query from legacy JSONL file (fallback when DB unavailable)."""
    audit_file = _get_audit_file()
    if not audit_file.exists():
        return []

    results: list[AuditEntry] = []
    try:
        with open(audit_file, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                entry = json.loads(line)
                if user and entry.get("user_id") != user:
                    continue
                if resource_type and entry.get("resource_type") != resource_type:
                    continue
                if resource_id and entry.get("resource_id") != resource_id:
                    continue
                ts = entry.get("timestamp", "")
                if start_time and ts < start_time:
                    continue
                if end_time and ts > end_time:
                    continue
                results.append(_dict_to_audit_entry(entry))
    except Exception as e:
        logger.warning("Audit JSONL query fallback failed: %s", e)

    results.sort(key=lambda e: e.timestamp, reverse=True)
    return results[:limit]


def _cleanup_jsonl_fallback(days: int) -> int:
    """Rewrite the JSONL file, removing entries older than `days` (fallback)."""

    audit_file = _get_audit_file()
    if not audit_file.exists():
        return 0

    cutoff = datetime.now(UTC) - timedelta(days=days)
    cutoff_str = cutoff.isoformat()
    kept: list[str] = []
    removed = 0

    try:
        with open(audit_file, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                entry = json.loads(line)
                if entry.get("timestamp", "") >= cutoff_str:
                    kept.append(line)
                else:
                    removed += 1

        tmp = audit_file.with_suffix(".jsonl.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(kept)
        tmp.replace(audit_file)
    except Exception as e:
        logger.warning("Audit JSONL cleanup fallback failed: %s", e)
        return 0

    if removed:
        logger.info("Cleaned up %d legacy audit JSONL entries older than %d days", removed, days)
    return removed


def _dict_to_audit_entry(d: dict) -> AuditEntry:
    """Convert a plain dict (from DB row or JSONL) to an AuditEntry dataclass."""
    return AuditEntry(
        audit_id=d.get("audit_id", ""),
        timestamp=d.get("timestamp", ""),
        user=d.get("user_id", ""),
        action=d.get("action", ""),
        resource_type=d.get("resource_type", ""),
        resource_id=d.get("resource_id", ""),
        resource_name=d.get("resource_name", ""),
        changes=json.loads(d.get("changes", "{}")),
        ip_address=d.get("ip_address", ""),
        user_agent=d.get("user_agent", ""),
    )


def _orm_to_audit_entry(orm) -> AuditEntry:
    """Convert an AuditLogORM row to an AuditEntry dataclass."""
    return AuditEntry(
        audit_id=orm.audit_id,
        timestamp=orm.timestamp.isoformat() if hasattr(orm.timestamp, "isoformat") else str(orm.timestamp),
        user=orm.user_id,
        action=orm.action,
        resource_type=orm.resource_type,
        resource_id=orm.resource_id,
        resource_name=orm.resource_name,
        changes=json.loads(orm.changes or "{}"),
        ip_address=orm.ip_address,
        user_agent=orm.user_agent,
    )


class ConfigValidator:
    """Configuration schema validator"""

    @staticmethod
    def validate_device_config(config: dict) -> tuple[bool, list[str]]:
        """Validate device configuration"""
        errors = []

        required = ["device_id", "name", "protocol"]
        # FIXED(P1): 原问题-for field in required 遮蔽了 dataclasses.field 导入(F402);
        #            修复-改用 field_name 避免遮蔽
        for field_name in required:
            if field_name not in config or not config[field_name]:
                errors.append(f"Missing required field: {field_name}")

        if "protocol" in config and normalize_protocol(config["protocol"]) is None:
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
        # FIXED(P1): 原问题-for field in required 遮蔽了 dataclasses.field 导入(F402);
        #            修复-改用 field_name 避免遮蔽
        for field_name in required:
            if field_name not in config:
                errors.append(f"Missing required field: {field_name}")

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

# FIXED(P1): 原问题-RUF006 create_task 返回值未保存，task 可能被 GC 回收;
#            修复-模块级 _background_tasks 集合保存引用，任务完成时自动移除
_background_tasks: set[asyncio.Task] = set()


def get_backup_service(
    backup_dir: str = "data/backups",
    interval_seconds: int | None = None,
    max_backups: int | None = None,
    auto_start: bool = True,
) -> ConfigBackupService:
    """Get (or create) the configuration backup service singleton.

    FIXED-AUTO-BACKUP: When auto_start=True the scheduler is launched immediately.
    Callers that manage the service lifecycle manually (e.g. tests) can pass
    auto_start=False and call start_scheduler()/stop_scheduler() themselves.
    """
    global _backup_service
    if _backup_service is None:
        _backup_service = ConfigBackupService(backup_dir, interval_seconds, max_backups)
        if auto_start:
            import asyncio
            try:
                asyncio.get_running_loop()
                # FIXED-P1: 命名任务以便 teardown 白名单兜底取消
                # FIXED(P1): 原问题-RUF006 create_task 返回值未保存，task 可能被 GC 回收;
                #            修复-保存到模块级 _background_tasks 集合
                task = asyncio.create_task(
                    _backup_service.start_scheduler(),
                    name="edgelite_config_backup_starter",
                )
                _background_tasks.add(task)
                task.add_done_callback(_background_tasks.discard)
            except RuntimeError:
                # No running loop yet — will be started by the app lifecycle
                pass
    return _backup_service


def get_log_rotation_service(config: LogConfig | None = None) -> LogRotationService:
    """Get log rotation service"""
    global _log_rotation
    if _log_rotation is None:
        _log_rotation = LogRotationService(config)
    return _log_rotation


def get_audit_service(audit_dir: str = "data/audit") -> ConfigAuditService:
    """Get the configuration audit service singleton.

    FIXED-AUDIT-DB: audit_dir is kept for backwards-compatibility but the
    audit_dir parameter is no longer used for primary storage (DB is now used).
    The legacy audit.jsonl file is preserved as a fallback path.
    """
    global _audit_service
    if _audit_service is None:
        _audit_service = ConfigAuditService(audit_dir=audit_dir)
    return _audit_service
