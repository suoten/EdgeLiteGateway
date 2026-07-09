"""系统管理业务逻辑"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re  # FIXED-P2: 原代码在 list_backups 循环内 import re，应提到模块顶部
import time
from datetime import UTC, datetime
from pathlib import Path

try:
    import psutil
except ImportError:
    psutil = None

import contextlib

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

    async def collect_resources(self) -> dict:
        loop = asyncio.get_running_loop()
        if psutil:
            cpu_percent = await loop.run_in_executor(None, lambda: psutil.cpu_percent(interval=0.1))
            cpu_count = psutil.cpu_count()
            cpu_count_logical = psutil.cpu_count(logical=True)
            memory = await loop.run_in_executor(None, psutil.virtual_memory)
            disk = await loop.run_in_executor(
                None, lambda: psutil.disk_usage("C:\\" if os.name == "nt" else "/")
            )
            net_counters = await loop.run_in_executor(None, psutil.net_io_counters)
            load_avg = await loop.run_in_executor(None, self._get_load_avg)
        else:
            cpu_percent = 0.0
            cpu_count = 0
            cpu_count_logical = 0
            memory = type("", (), {"total": 0, "used": 0, "percent": 0.0, "available": 0})()
            disk = type("", (), {"total": 0, "used": 0, "percent": 0.0, "free": 0})()
            net_counters = type("", (), {"bytes_sent": 0, "bytes_recv": 0})()
            load_avg = (0.0, 0.0, 0.0)

        return {
            "cpu_percent": cpu_percent,
            "cpu_count": cpu_count,
            "cpu_count_logical": cpu_count_logical,
            "memory_total": memory.total,
            "memory_used": memory.used,
            "memory_available": getattr(memory, "available", 0),
            "memory_percent": memory.percent,
            "disk_total": disk.total,
            "disk_used": disk.used,
            "disk_free": getattr(disk, "free", 0),
            "disk_percent": disk.percent,
            "net_bytes_sent": getattr(net_counters, "bytes_sent", 0),
            "net_bytes_recv": getattr(net_counters, "bytes_recv", 0),
            "load_avg_1m": load_avg[0],
            "load_avg_5m": load_avg[1],
            "load_avg_15m": load_avg[2],
            "collected_at": datetime.now(UTC).isoformat(),
        }

    @staticmethod
    def _get_load_avg() -> tuple[float, float, float]:
        try:
            import os as _os
            if hasattr(_os, "getloadavg"):
                return _os.getloadavg()
        except Exception as e:
            # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
            logger.warning("获取系统负载失败: %s", e)
        return (0.0, 0.0, 0.0)

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
        _devices, device_total = await self._device_repo.list_all(page=1, size=1)  # FIXED(P3): 原问题-解包变量devices未使用; 修复-改为_devices前缀
        # FIXED-P0: get_active_devices改为async，需await
        online_devices = len(await self._scheduler.get_active_devices())

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
            "collect_task_count": await self._scheduler.get_task_count(),
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

        # FIXED(严重): 备份文件明文存储敏感信息 - SQLite 备份包含用户密码哈希、设备通信密码、
        # JWT 密钥等敏感数据，设置权限为 0o600（仅 owner 可读写），防止其他用户读取
        try:
            os.chmod(backup_file, 0o600)
        except OSError as chmod_err:
            logger.warning("设置主备份文件权限失败: %s", chmod_err)

        # FIXED-P1: 原问题-备份不包含辅助DB，恢复后时序数据/边缘规则/审计日志丢失
        # 复制辅助DB文件到备份目录
        data_dir = Path(config.database.sqlite_path).parent if config.database.sqlite_path else Path("data")
        _AUX_DB_FILES = [
            "edgelite_ts.db", "edge_rules.db", "edge_triggers.db",
            "audit.db", "device_status.db", "mqtt_offline_queue.db",
            "emergency_buffer.db", "opcua_ts.db", "opcua_config_versions.db",
        ]
        aux_backed_up: list[str] = []
        loop = asyncio.get_running_loop()
        for aux_name in _AUX_DB_FILES:
            aux_path = data_dir / aux_name
            if aux_path.exists():
                try:
                    import shutil
                    # FIXED-P1: 原代码 shutil.copy2 是同步阻塞操作，在异步函数中会阻塞事件循环
                    # 改为使用 run_in_executor 在线程池中执行
                    aux_backup_path = backup_dir / f"backup_{timestamp}_{aux_name}"
                    await loop.run_in_executor(
                        None,
                        shutil.copy2,
                        str(aux_path),
                        str(aux_backup_path),
                    )
                    # FIXED(严重): 辅助DB备份文件同样包含敏感信息（如 audit.db 的哈希链、
                    # opcua_config_versions.db 的配置快照），设置权限为 0o600 防止泄露
                    try:
                        os.chmod(aux_backup_path, 0o600)
                    except OSError as chmod_err:
                        logger.warning("设置辅助备份文件 %s 权限失败: %s", aux_name, chmod_err)
                    aux_backed_up.append(aux_name)
                except Exception as e:
                    logger.warning("Auxiliary DB backup failed for %s: %s", aux_name, e)

        json_file = backup_dir / f"backup_{timestamp}.json"
        try:
            backup_data = await self._export_all_config()
            # FIXED-P1: 同步文件 I/O 包装到线程中，避免阻塞事件循环
            def _write_backup_json(path, data):
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            await asyncio.to_thread(_write_backup_json, json_file, backup_data)
        except Exception as e:
            logger.error("配置导出失败: %s", e)
            raise RuntimeError(f"Config export failed: {e}") from e  # FIXED: 原问题-中文硬编码错误消息

        return {
            "backup_id": timestamp,
            "db_file": str(backup_file),
            "json_file": str(json_file),
            "aux_dbs": aux_backed_up,
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
            backup_id = f.stem.replace("backup_", "")
            # Validate backup_id format (should be a timestamp like YYYYMMDD_HHMMSS)
            # FIXED-P2: import re 已移至模块顶部
            if not re.match(r"^\d{8}_\d{6}$", backup_id):
                logger.debug("Skipping non-backup file with invalid ID: %s (backup_id=%s)", f.name, backup_id)
                continue
            backups.append(
                {
                    "backup_id": backup_id,
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
            # FIXED-P1: 同步文件 I/O 包装到线程中，避免阻塞事件循环
            def _read_backup_json(path):
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
            backup_data = await asyncio.to_thread(_read_backup_json, json_file)
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Backup file read failed: %s", e)
            raise RuntimeError(f"Backup file corrupted or unreadable: {e}") from e

        # FIXED-P0: 验证备份文件结构完整性，防止格式错误的备份导致恢复异常
        if not isinstance(backup_data, dict):
            raise RuntimeError("Backup file format invalid: expected dict")
        if "devices" not in backup_data or "rules" not in backup_data:
            raise RuntimeError("Backup file format invalid: missing required keys (devices/rules)")
        for key in ("devices", "rules", "users"):
            section = backup_data.get(key, [])
            if not isinstance(section, list):
                raise RuntimeError(f"Backup file format invalid: '{key}' must be a list")

        restored_counts = {}
        # FIXED-P0: 原回滚逻辑有缺陷——更新已有记录时回滚会删除该记录导致数据丢失
        # 改为追踪操作类型（create/update），回滚时只删除新建的记录，更新的记录无法自动恢复原值
        restored_device_ids: list[tuple[str, str]] = []  # (device_id, operation: "create"/"update")
        restored_rule_ids: list[tuple[str, str]] = []
        restored_user_ids: list[tuple[str, str]] = []
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
                        restored_device_ids.append((dev_id, "update"))
                    else:
                        await self._device_repo.create(dev)
                        restored_device_ids.append((dev_id, "create"))
                restored_counts["devices"] = len(restored_device_ids)

            rules_data = backup_data.get("rules", [])
            if rules_data:
                for rule in rules_data:
                    rule_id = rule.get("rule_id")
                    if rule_id is None:
                        continue
                    existing = await self._rule_repo.get_by_id(rule_id)
                    if existing:
                        await self._rule_repo.update(rule_id, rule)
                        restored_rule_ids.append((rule_id, "update"))
                    else:
                        await self._rule_repo.create(rule)
                        restored_rule_ids.append((rule_id, "create"))
                restored_counts["rules"] = len(restored_rule_ids)

            users_data = backup_data.get("users", [])
            if users_data:
                for user in users_data:
                    user_id = user.get("user_id")
                    if user_id is None:
                        continue
                    existing = await self._user_repo.get_by_id(user_id)
                    if existing:
                        await self._user_repo.update(user_id, user)
                        restored_user_ids.append((user_id, "update"))
                    else:
                        await self._user_repo.create(user)
                        restored_user_ids.append((user_id, "create"))
                restored_counts["users"] = len(restored_user_ids)
        except Exception as e:
            # FIXED-P0: 回滚时只删除新建的记录，不删除已更新的记录（避免数据丢失）
            logger.error(
                "Restore backup data failed: %s, rolling back %d devices, %d rules, %d users (only created records will be deleted)",
                e, len(restored_device_ids), len(restored_rule_ids), len(restored_user_ids,
                ),
            )
            for uid, op in restored_user_ids:
                if op == "create":
                    with contextlib.suppress(Exception):
                        await self._user_repo.delete(uid)
            for rid, op in restored_rule_ids:
                if op == "create":
                    with contextlib.suppress(Exception):
                        await self._rule_repo.delete(rid)
            for did, op in restored_device_ids:
                if op == "create":
                    with contextlib.suppress(Exception):
                        await self._device_repo.delete(did)
            raise RuntimeError(f"Restore failed and rolled back (updated records kept, created records deleted): {e}") from e

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
        # FIXED: 原问题-只导出第1页，超过_EXPORT_QUERY_SIZE条时数据丢失；改为循环导出全量
        devices: list[dict] = []
        page = 1
        while True:
            batch, total = await self._device_repo.list_all(page=page, size=_EXPORT_QUERY_SIZE)
            devices.extend(batch)
            if len(devices) >= total or not batch:
                break
            page += 1

        rules: list[dict] = []
        page = 1
        while True:
            batch, total = await self._rule_repo.list_all(page=page, size=_EXPORT_QUERY_SIZE)
            rules.extend(batch)
            if len(rules) >= total or not batch:
                break
            page += 1

        users: list[dict] = []
        page = 1
        while True:
            batch, total = await self._user_repo.list_all(page=page, size=_EXPORT_QUERY_SIZE)
            users.extend(batch)
            if len(users) >= total or not batch:
                break
            page += 1

        _SENSITIVE_KEYS = {"password", "secret", "token", "private_key", "privateKey", "api_key", "apiKey", "secret_key", "secretKey", "cip_password"}  # FIXED-P4: 设备配置导出时脱敏敏感字段

        def _mask_device(d: dict) -> dict:
            config = d.get("config", {})
            if isinstance(config, dict):
                for key in list(config.keys()):
                    if key.lower() in _SENSITIVE_KEYS or any(s in key.lower() for s in _SENSITIVE_KEYS):
                        config[key] = "********"
            return d

        return {
            "version": "1.0.0",
            "exported_at": datetime.now(UTC).isoformat(),
            "devices": [_mask_device(d) for d in devices],
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
