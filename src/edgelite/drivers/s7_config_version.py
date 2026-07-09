"""S7 配置版本管理模块 — 记录配置变更历史，支持回滚和审计追踪。

内存存储配置版本快照，每个设备维护独立的版本链。
线程安全: 使用 threading.Lock 保护内部状态。
"""

from __future__ import annotations

import copy
import logging
import threading
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


class S7ConfigVersionManager:
    """S7 设备配置版本管理器

    为每个设备维护配置版本链，支持:
    - save_version: 保存新版本快照
    - get_current/get_versions: 查询版本信息
    - rollback: 回滚到指定版本
    - diff_versions: 比较两个版本差异
    - get_audit_trail: 获取审计追踪记录
    """

    def __init__(self) -> None:
        self._versions: dict[str, list[dict[str, Any]]] = {}
        self._audit_trail: dict[str, list[dict[str, Any]]] = []
        self._lock = threading.Lock()

    async def save_version(self, device_id: str, config: dict,
                           change_summary: str = "", operator: str = "system") -> dict[str, Any]:
        """保存配置版本快照

        Args:
            device_id: 设备唯一标识
            config: 配置字典 (深拷贝存储)
            change_summary: 变更摘要
            operator: 操作者

        Returns:
            版本信息字典 {version, device_id, timestamp, change_summary, operator}
        """
        with self._lock:
            versions = self._versions.setdefault(device_id, [])
            version_num = len(versions) + 1
            version_info = {
                "version": version_num,
                "device_id": device_id,
                "timestamp": datetime.now(UTC).isoformat(),
                "change_summary": change_summary,
                "operator": operator,
                "config": copy.deepcopy(config),
            }
            versions.append(version_info)
            self._audit_trail.setdefault(device_id, []).append({
                "version": version_num,
                "timestamp": version_info["timestamp"],
                "action": "save",
                "operator": operator,
                "summary": change_summary,
            })
        logger.info("[s7_config_version] device=%s version=%d saved", device_id, version_num)
        return {k: v for k, v in version_info.items() if k != "config"}

    async def get_current(self, device_id: str) -> dict[str, Any] | None:
        """获取设备当前 (最新) 版本信息 (不含 config)"""
        with self._lock:
            versions = self._versions.get(device_id, [])
            if not versions:
                return None
            latest = versions[-1]
            return {k: v for k, v in latest.items() if k != "config"}

    async def get_versions(self, device_id: str) -> list[dict[str, Any]]:
        """获取设备所有版本列表 (不含 config)"""
        with self._lock:
            versions = self._versions.get(device_id, [])
            return [{k: v for k, v in v.items() if k != "config"} for v in versions]

    async def get_version_config(self, device_id: str, version: int) -> dict[str, Any] | None:
        """获取指定版本的完整配置"""
        with self._lock:
            versions = self._versions.get(device_id, [])
            for v in versions:
                if v["version"] == version:
                    return copy.deepcopy(v.get("config", {}))
            return None

    async def rollback(self, device_id: str, target_version: int,
                       operator: str = "system") -> dict[str, Any]:
        """回滚到指定版本 (创建新版本，内容复制自目标版本)

        Args:
            device_id: 设备唯一标识
            target_version: 目标版本号
            operator: 操作者

        Returns:
            新版本信息

        Raises:
            ValueError: 目标版本不存在
        """
        with self._lock:
            versions = self._versions.setdefault(device_id, [])
            target_config = None
            for v in versions:
                if v["version"] == target_version:
                    target_config = copy.deepcopy(v.get("config", {}))
                    break
            if target_config is None:
                raise ValueError(f"Version {target_version} not found for device {device_id}")
            version_num = len(versions) + 1
            version_info = {
                "version": version_num,
                "device_id": device_id,
                "timestamp": datetime.now(UTC).isoformat(),
                "change_summary": f"Rollback to version {target_version}",
                "operator": operator,
                "config": target_config,
            }
            versions.append(version_info)
            self._audit_trail.setdefault(device_id, []).append({
                "version": version_num,
                "timestamp": version_info["timestamp"],
                "action": "rollback",
                "operator": operator,
                "summary": f"Rollback to version {target_version}",
            })
        logger.info("[s7_config_version] device=%s rollback to v%d (new v%d)",
                    device_id, target_version, version_num)
        return {k: v for k, v in version_info.items() if k != "config"}

    async def get_audit_trail(self, device_id: str, limit: int = 100) -> list[dict[str, Any]]:
        """获取设备审计追踪记录"""
        with self._lock:
            trail = self._audit_trail.get(device_id, [])
            return list(trail[-limit:])

    def diff_versions(self, device_id: str, version_a: int, version_b: int) -> dict[str, Any]:
        """比较两个版本的配置差异

        Returns:
            {only_in_a: [...], only_in_b: [...], changed: [...], value_a: {...}, value_b: {...}}
        """
        with self._lock:
            versions = self._versions.get(device_id, [])
            config_a = None
            config_b = None
            for v in versions:
                if v["version"] == version_a:
                    config_a = v.get("config", {})
                if v["version"] == version_b:
                    config_b = v.get("config", {})
        if config_a is None or config_b is None:
            raise ValueError(f"Version(s) not found: {version_a}, {version_b}")
        keys_a = set(config_a.keys())
        keys_b = set(config_b.keys())
        changed = [k for k in keys_a & keys_b if config_a[k] != config_b[k]]
        return {
            "only_in_a": sorted(keys_a - keys_b),
            "only_in_b": sorted(keys_b - keys_a),
            "changed": sorted(changed),
            "version_a": version_a,
            "version_b": version_b,
        }

    async def stop(self) -> None:
        """停止管理器 (内存模式无需清理资源)"""
        logger.debug("[s7_config_version] stopped")
