"""S7 OTA 升级管理模块 — 管理固件升级流程，支持检查、执行、回滚。

提供 OTA 升级状态机和进度追踪，升级记录持久化到内存。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


class OtaStatus(StrEnum):
    """OTA 升级状态枚举"""

    IDLE = "idle"
    CHECKING = "checking"
    READY = "ready"
    DOWNLOADING = "downloading"
    INSTALLING = "installing"
    COMPLETED = "completed"
    FAILED = "failed"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"


@dataclass
class OtaProgress:
    """OTA 升级进度"""

    status: OtaStatus = OtaStatus.IDLE
    progress: float = 0.0  # 0.0 - 1.0
    message: str = ""
    started_at: str = ""
    completed_at: str = ""


@dataclass
class OtaHistoryEntry:
    """OTA 历史记录条目"""

    timestamp: str
    package: str
    status: str
    message: str = ""


class S7OtaManager:
    """S7 PLC OTA 升级管理器

    管理固件升级生命周期: 检查更新 → 下载 → 安装 → 完成/回滚
    """

    def __init__(self) -> None:
        self._current_version: str = "1.0.0"
        self._progress = OtaProgress()
        self._history: list[OtaHistoryEntry] = []
        self._lock = threading.Lock()
        self._last_pkg: dict[str, Any] | None = None

    def set_current_version(self, version: str) -> None:
        """设置当前固件版本"""
        self._current_version = str(version)
        logger.info("[s7_ota] current_version=%s", self._current_version)

    async def check_update(self, pkg: dict[str, Any]) -> dict[str, Any]:
        """检查是否有可用更新

        Args:
            pkg: 升级包信息 {version, url, checksum, ...}

        Returns:
            {has_update, current_version, target_version, message}
        """
        with self._lock:
            self._progress.status = OtaStatus.CHECKING
            self._progress.message = "Checking for updates"
        target_version = str(pkg.get("version", ""))
        has_update = bool(target_version) and target_version != self._current_version
        result = {
            "has_update": has_update,
            "current_version": self._current_version,
            "target_version": target_version,
            "message": f"Update available: {target_version}" if has_update else "Already up to date",
        }
        with self._lock:
            if has_update:
                self._progress.status = OtaStatus.READY
                self._progress.message = f"Update ready: {target_version}"
                self._last_pkg = pkg
            else:
                self._progress.status = OtaStatus.IDLE
                self._progress.message = "No update available"
        return result

    async def start_ota(self, pkg: dict[str, Any], config_snapshot: dict | None = None) -> dict[str, Any]:
        """启动 OTA 升级流程

        Args:
            pkg: 升级包信息
            config_snapshot: 升级前配置快照 (用于回滚)

        Returns:
            {success, message, progress}
        """
        with self._lock:
            self._progress.status = OtaStatus.DOWNLOADING
            self._progress.progress = 0.0
            self._progress.started_at = datetime.now(UTC).isoformat()
            self._progress.message = "Starting OTA upgrade"
            self._last_pkg = pkg
        # 模拟升级流程 (实际实现需要与 PLC 交互)
        target_version = str(pkg.get("version", ""))
        try:
            with self._lock:
                self._progress.status = OtaStatus.INSTALLING
                self._progress.progress = 0.5
                self._progress.message = f"Installing version {target_version}"
            # 模拟安装完成
            old_version = self._current_version
            self._current_version = target_version
            with self._lock:
                self._progress.status = OtaStatus.COMPLETED
                self._progress.progress = 1.0
                self._progress.completed_at = datetime.now(UTC).isoformat()
                self._progress.message = f"Upgraded {old_version} -> {target_version}"
                self._history.append(
                    OtaHistoryEntry(
                        timestamp=self._progress.completed_at,
                        package=target_version,
                        status=OtaStatus.COMPLETED.value,
                        message=self._progress.message,
                    )
                )
            logger.info("[s7_ota] OTA completed: %s -> %s", old_version, target_version)
            return {"success": True, "message": self._progress.message, "progress": 1.0}
        except Exception as e:
            with self._lock:
                self._progress.status = OtaStatus.FAILED
                self._progress.message = f"OTA failed: {e}"
                self._history.append(
                    OtaHistoryEntry(
                        timestamp=datetime.now(UTC).isoformat(),
                        package=target_version,
                        status=OtaStatus.FAILED.value,
                        message=str(e),
                    )
                )
            logger.error("[s7_ota] OTA failed: %s", e)
            return {"success": False, "message": str(e), "progress": self._progress.progress}

    async def rollback_ota(self) -> dict[str, Any]:
        """回滚到上一个版本

        Returns:
            {success, message}
        """
        with self._lock:
            self._progress.status = OtaStatus.ROLLING_BACK
            self._progress.message = "Rolling back OTA"
        # 模拟回滚 (实际实现需要恢复备份固件)
        with self._lock:
            self._progress.status = OtaStatus.ROLLED_BACK
            self._progress.message = "OTA rolled back"
            self._history.append(
                OtaHistoryEntry(
                    timestamp=datetime.now(UTC).isoformat(),
                    package=self._current_version,
                    status=OtaStatus.ROLLED_BACK.value,
                    message="Manual rollback",
                )
            )
        logger.info("[s7_ota] OTA rolled back")
        return {"success": True, "message": self._progress.message}

    def get_progress(self) -> dict[str, Any]:
        """获取当前 OTA 进度"""
        with self._lock:
            return {
                "status": self._progress.status.value,
                "progress": self._progress.progress,
                "message": self._progress.message,
                "started_at": self._progress.started_at,
                "completed_at": self._progress.completed_at,
                "current_version": self._current_version,
            }

    def get_history(self, limit: int = 50) -> list[dict[str, Any]]:
        """获取 OTA 历史记录"""
        with self._lock:
            entries = self._history[-limit:]
        return [
            {"timestamp": e.timestamp, "package": e.package, "status": e.status, "message": e.message} for e in entries
        ]
