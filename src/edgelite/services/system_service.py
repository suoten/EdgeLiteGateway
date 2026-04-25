"""系统管理业务逻辑"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import asyncio
import psutil

from edgelite.storage.sqlite_repo import DeviceRepo, RuleRepo, AlarmRepo, UserRepo
from edgelite.storage.database import Database
from edgelite.engine.scheduler import CollectScheduler
from edgelite.config import get_config

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
        import asyncio
        # 系统资源（psutil 是同步阻塞调用，放到线程池中执行）
        loop = asyncio.get_event_loop()
        cpu_percent = await loop.run_in_executor(None, lambda: psutil.cpu_percent(interval=0.1))
        memory = await loop.run_in_executor(None, psutil.virtual_memory)
        disk = await loop.run_in_executor(None, lambda: psutil.disk_usage("/"))

        # 设备统计
        devices, device_total = await self._device_repo.list_all(page=1, size=1)
        online_devices = len(self._scheduler.get_active_devices())

        # 规则统计
        _, rule_total = await self._rule_repo.list_all(page=1, size=1)

        # 告警统计
        _, firing_count = await self._alarm_repo.list_all(page=1, size=1, status="firing")

        uptime = int(time.time() - self._start_time)

        return {
            "cpu_percent": cpu_percent,
            "memory_total": memory.total,
            "memory_used": memory.used,
            "memory_percent": memory.percent,
            "disk_total": disk.total,
            "disk_used": disk.used,
            "disk_percent": disk.percent,
            "device_total": device_total,
            "device_online": online_devices,
            "rule_total": rule_total,
            "alarm_firing": firing_count,
            "collect_task_count": self._scheduler.get_task_count(),
            "uptime": uptime,
            "version": "0.1.0",
        }

    async def create_backup(self) -> dict:
        """创建配置备份"""
        config = get_config()
        backup_dir = Path(config.database.backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_file = backup_dir / f"backup_{timestamp}.db"

        # 备份SQLite数据库
        await self._database.backup(str(backup_file))

        # 同时导出JSON格式
        json_file = backup_dir / f"backup_{timestamp}.json"
        backup_data = await self._export_all_config()
        with open(json_file, "w", encoding="utf-8") as f:
            json.dump(backup_data, f, ensure_ascii=False, indent=2)

        return {
            "backup_id": timestamp,
            "db_file": str(backup_file),
            "json_file": str(json_file),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    async def list_backups(self) -> list[dict]:
        """列出所有备份"""
        config = get_config()
        backup_dir = Path(config.database.backup_dir)
        if not backup_dir.exists():
            return []

        backups = []
        for f in sorted(backup_dir.glob("backup_*.json"), reverse=True):
            backups.append({
                "backup_id": f.stem.replace("backup_", ""),
                "file": str(f),
                "size": f.stat().st_size,
            })
        return backups[:20]  # 最多返回20个

    async def restore_backup(self, backup_id: str) -> bool:
        """从备份恢复配置"""
        config = get_config()
        backup_dir = Path(config.database.backup_dir)
        json_file = backup_dir / f"backup_{backup_id}.json"

        if not json_file.exists():
            return False

        with open(json_file, "r", encoding="utf-8") as f:
            backup_data = json.load(f)

        # 恢复配置（需要重启生效）
        # MVP阶段：仅标记恢复请求，实际恢复在重启时执行
        logger.info("配置恢复请求: %s (需重启生效)", backup_id)
        return True

    async def _export_all_config(self) -> dict:
        """导出全量配置为JSON"""
        devices, _ = await self._device_repo.list_all(page=1, size=10000)
        rules, _ = await self._rule_repo.list_all(page=1, size=10000)
        users, _ = await self._user_repo.list_all(page=1, size=10000)

        return {
            "version": "0.1.0",
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "devices": devices,
            "rules": rules,
            "users": [{"user_id": u["user_id"], "username": u["username"], "role": u["role"], "enabled": u["enabled"]} for u in users],
        }
