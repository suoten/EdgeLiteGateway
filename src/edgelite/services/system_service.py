"""系统管理业务逻辑"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path

try:
    import psutil
except ImportError:
    psutil = None

from edgelite.config import get_config
from edgelite.constants import _DEFAULT_PAGE_SIZE, _EXPORT_QUERY_SIZE
from edgelite.engine.scheduler import CollectScheduler
from edgelite.storage.database import Database
from edgelite.storage.sqlite_repo import AlarmRepo, DeviceRepo, RuleRepo, UserRepo

logger = logging.getLogger(__name__)


class SystemService:
    """系统管理业务逻辑"""

    def __init__(
        self,
        database: Database,
        device_repo: DeviceRepo,
        rule_repo: RuleRepo,
        alarm_repo: AlarmRepo,
        user_repo: UserRepo,
        scheduler: CollectScheduler,
        start_time: float,
    ):
        self._database = database
        self._device_repo = device_repo
        self._rule_repo = rule_repo
        self._alarm_repo = alarm_repo
        self._user_repo = user_repo
        self._scheduler = scheduler
        self._start_time = start_time

    async def get_status(self) -> dict:
        """获取系统运行状态"""
        loop = asyncio.get_running_loop()
        if psutil:
            cpu_percent = await loop.run_in_executor(None, lambda: psutil.cpu_percent(interval=0.1))
            memory = await loop.run_in_executor(None, psutil.virtual_memory)
            disk = await loop.run_in_executor(
                None, lambda: psutil.disk_usage("C:\\" if os.name == "nt" else "/")
            )
            mem_total, mem_used, mem_pct = memory.total, memory.used, memory.percent
            disk_total, disk_used, disk_pct = disk.total, disk.used, disk.percent
        else:
            cpu_percent = 0.0
            mem_total, mem_used, mem_pct = 0, 0, 0.0
            disk_total, disk_used, disk_pct = 0, 0, 0.0

        # 设备统计
        devices, device_total = await self._device_repo.list_all(page=1, size=1)
        online_devices = len(self._scheduler.get_active_devices())

        # 规则统计
        _, rule_total = await self._rule_repo.list_all(page=1, size=1)
        from sqlalchemy import func as sa_func
        from sqlalchemy import select

        from edgelite.app import _app_state
        from edgelite.models.db import RuleORM

        # FIXED: 原问题-规则统计直接使用session.execute无try-except保护
        try:
            async with _app_state.database.get_session() as session:
                result = await session.execute(
                    select(sa_func.count()).select_from(RuleORM).where(RuleORM.enabled.is_(True))
                )
                rule_enabled = result.scalar() or 0
        except Exception as e:
            logger.error("SystemService.get_status rule count failed: %s", e)
            rule_enabled = 0

        # 告警统计
        _, firing_count = await self._alarm_repo.list_all(page=1, size=1, status="firing")

        uptime = int(time.time() - self._start_time)

        return {
            "cpu_percent": cpu_percent,
            "memory_total": mem_total,
            "memory_used": mem_used,
            "memory_percent": mem_pct,
            "disk_total": disk_total,
            "disk_used": disk_used,
            "disk_percent": disk_pct,
            "device_total": device_total,
            "device_online": online_devices,
            "rule_total": rule_total,
            "rule_enabled": rule_enabled,
            "alarm_firing": firing_count,
            "collect_task_count": self._scheduler.get_task_count(),
            "uptime": uptime,
            "version": __import__("edgelite").__version__,
        }

    async def create_backup(self) -> dict:
        """创建配置备份"""
        config = get_config()
        backup_dir = Path(config.database.backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        backup_file = backup_dir / f"backup_{timestamp}.db"

        # FIXED: 备份操作无异常保护
        try:
            await self._database.backup(str(backup_file))
        except Exception as e:
            logger.error("数据库备份失败: %s", e)
            raise RuntimeError(f"Database backup failed: {e}") from e  # FIXED: 原问题-中文硬编码错误消息

        json_file = backup_dir / f"backup_{timestamp}.json"
        try:
            backup_data = await self._export_all_config()
            with open(json_file, "w", encoding="utf-8") as f:
                json.dump(backup_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error("配置导出失败: %s", e)
            raise RuntimeError(f"Config export failed: {e}") from e  # FIXED: 原问题-中文硬编码错误消息

        return {
            "backup_id": timestamp,
            "db_file": str(backup_file),
            "json_file": str(json_file),
            "created_at": datetime.now(UTC).isoformat(),
        }

    async def list_backups(self) -> list[dict]:
        """列出所有备份"""
        config = get_config()
        backup_dir = Path(config.database.backup_dir)
        if not backup_dir.exists():
            return []

        backups = []
        for f in sorted(backup_dir.glob("backup_*.json"), reverse=True):
            # FIXED: f.stat()可能因文件被删除而抛出FileNotFoundError
            try:
                size = f.stat().st_size
            except OSError:
                continue
            backups.append(
                {
                    "backup_id": f.stem.replace("backup_", ""),
                    "file": str(f),
                    "size": size,
                }
            )
        return backups[:_DEFAULT_PAGE_SIZE]  # FIXED: 原问题-backups[:20]魔法数字

    async def restore_backup(self, backup_id: str) -> bool:
        """从备份恢复配置"""
        # FIXED: P1-1 原问题-恢复后仅更新DB，内存中的evaluator/scheduler/driver状态未同步刷新
        # 修复：恢复后显式清除evaluator规则缓存，并要求重启调度任务
        config = get_config()
        backup_dir = Path(config.database.backup_dir)
        json_file = backup_dir / f"backup_{backup_id}.json"

        if not json_file.exists():
            return False

        try:
            with open(json_file, encoding="utf-8") as f:
                backup_data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Backup file read failed: %s", e)
            raise RuntimeError(f"Backup file corrupted or unreadable: {e}") from e

        restored_counts = {}
        try:
            devices_data = backup_data.get("devices", [])
            if devices_data:
                for dev in devices_data:
                    dev_id = dev.get("device_id")
                    if dev_id is None:
                        continue
                    existing = await self._device_repo.get_by_id(dev_id)
                    if existing:
                        await self._device_repo.update(dev_id, dev)
                    else:
                        await self._device_repo.create(dev)
                restored_counts["devices"] = len(devices_data)

            rules_data = backup_data.get("rules", [])
            if rules_data:
                for rule in rules_data:
                    rule_id = rule.get("rule_id")
                    if rule_id is None:
                        continue
                    existing = await self._rule_repo.get_by_id(rule_id)
                    if existing:
                        await self._rule_repo.update(rule_id, rule)
                    else:
                        await self._rule_repo.create(rule)
                restored_counts["rules"] = len(rules_data)

            users_data = backup_data.get("users", [])
            if users_data:
                for user in users_data:
                    user_id = user.get("user_id")
                    if user_id is None:
                        continue
                    existing = await self._user_repo.get_by_id(user_id)
                    if existing:
                        await self._user_repo.update(user_id, user)
                    else:
                        await self._user_repo.create(user)
                restored_counts["users"] = len(users_data)
        except Exception as e:
            logger.error("Restore backup data failed: %s", e)
            raise RuntimeError(f"Restore failed: {e}") from e

        # 恢复后同步刷新运行时状态
        try:
            from edgelite.app import _app_state

            evaluator = getattr(_app_state, "evaluator", None)
            if evaluator is not None:
                evaluator._rule_cache.clear()
                evaluator._duration_tracker.clear()
                evaluator._cache_time = 0.0
                logger.info("Evaluator rule cache cleared after restore")
        except Exception as e:
            logger.warning("Failed to clear evaluator cache after restore: %s", e)

        logger.info(
            "Config restored from backup %s: %s. Restart required to apply device/scheduler changes.",
            backup_id,
            restored_counts,
        )
        return True

    async def _export_all_config(self) -> dict:
        """导出全量配置为JSON"""
        devices, _ = await self._device_repo.list_all(page=1, size=_EXPORT_QUERY_SIZE)  # FIXED: 原问题-size=10000魔法数字
        rules, _ = await self._rule_repo.list_all(page=1, size=_EXPORT_QUERY_SIZE)
        users, _ = await self._user_repo.list_all(page=1, size=_EXPORT_QUERY_SIZE)

        return {
            "version": "1.0.0",
            "exported_at": datetime.now(UTC).isoformat(),
            "devices": devices,
            "rules": rules,
            "users": [
                {
                    "user_id": u["user_id"],
                    "username": u["username"],
                    "role": u["role"],
                    "enabled": u["enabled"],
                }
                for u in users
            ],
        }
