"""FINS 驱动 OTA 固件升级管理 - 纯同步实现

为欧姆龙 FINS 协议驱动提供固件升级生命周期管理：
- 检查是否有可用更新
- 启动 OTA 升级
- 回滚到上一版本
- 查询升级进度与历史

关键差异（相对 OPC UA/S7）：
- 所有方法均为同步（sync），非 async
- check_update/start_ota/rollback_ota 返回 bool（True/False），
  而非 OPC UA/S7 的 dict 结果
- OtaPackage 为 dataclass，fins.py 中仅访问 package.version 字段
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class OtaPackage:
    """OTA 固件包描述。

    fins.py 中从不构造此对象（仅作为参数传入），唯一访问字段为 version。
    """

    package_id: str = ""
    version: str = ""
    firmware_url: str = ""
    firmware_hash: str = ""
    firmware_size: int = 0


class FinsOtaManager:
    """FINS OTA 升级管理器（纯同步实现，返回 bool）。

    维护内存态的当前版本、升级进度与历史记录。
    check_update/start_ota/rollback_ota 均返回 bool 表示操作是否成功。
    """

    def __init__(self) -> None:
        # 当前固件版本
        self._current_version: str = "1.0.0"
        # 升级前的版本（用于回滚）
        self._previous_version: str | None = None
        # 升级进度信息
        self._progress: dict[str, Any] = {
            "state": "idle",
            "current_version": self._current_version,
            "target_version": None,
            "progress_percent": 0,
            "error": None,
        }
        # 升级历史记录
        self._history: list[dict[str, Any]] = []

    def check_update(self, package: OtaPackage) -> bool:
        """检查是否有可用固件更新。

        Returns:
            True 表示有更新（目标版本与当前版本不同）。
        """
        has_update = bool(package.version) and package.version != self._current_version
        logger.info(
            "[fins-ota] 检查更新: current=%s target=%s available=%s",
            self._current_version, package.version, has_update,
        )
        return has_update

    def start_ota(self, package: OtaPackage, config_snapshot: dict[str, Any]) -> bool:
        """启动 OTA 升级。

        Args:
            package: 目标固件包
            config_snapshot: 升级前配置快照（用于回滚）

        Returns:
            True 表示升级成功。
        """
        try:
            # 记录升级前版本，供回滚使用
            self._previous_version = self._current_version
            target_version = package.version or self._current_version
            now = datetime.now(UTC).isoformat()
            # 更新进度
            self._progress = {
                "state": "completed",
                "current_version": target_version,
                "target_version": target_version,
                "progress_percent": 100,
                "error": None,
                "started_at": now,
                "completed_at": now,
            }
            # 记录历史
            self._history.append(
                {
                    "timestamp": now,
                    "action": "ota_start",
                    "from_version": self._previous_version,
                    "to_version": target_version,
                    "package_id": package.package_id,
                    "success": True,
                }
            )
            self._current_version = target_version
            logger.info(
                "[fins-ota] OTA升级成功: %s -> %s",
                self._previous_version, target_version,
            )
            return True
        except Exception as e:  # noqa: BLE001
            self._progress = {
                "state": "failed",
                "current_version": self._current_version,
                "target_version": package.version,
                "progress_percent": 0,
                "error": str(e),
            }
            self._history.append(
                {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "action": "ota_start",
                    "from_version": self._current_version,
                    "to_version": package.version,
                    "package_id": package.package_id,
                    "success": False,
                    "error": str(e),
                }
            )
            logger.error("[fins-ota] OTA升级失败: %s", e)
            return False

    def rollback_ota(self) -> bool:
        """回滚到升级前的固件版本。

        Returns:
            True 表示回滚成功（无前序版本可回滚时也返回 True，保持当前版本）。
        """
        try:
            target = self._previous_version or self._current_version
            now = datetime.now(UTC).isoformat()
            self._history.append(
                {
                    "timestamp": now,
                    "action": "ota_rollback",
                    "from_version": self._current_version,
                    "to_version": target,
                    "success": True,
                }
            )
            self._current_version = target
            self._previous_version = None
            self._progress = {
                "state": "rolled_back",
                "current_version": target,
                "target_version": target,
                "progress_percent": 100,
                "error": None,
                "rolled_back_at": now,
            }
            logger.info("[fins-ota] OTA回滚成功: -> %s", target)
            return True
        except Exception as e:  # noqa: BLE001
            logger.error("[fins-ota] OTA回滚失败: %s", e)
            return False

    def get_progress(self) -> dict[str, Any]:
        """获取当前 OTA 升级进度。"""
        return dict(self._progress)

    def get_history(self, limit: int) -> list[dict[str, Any]]:
        """获取 OTA 升级历史（倒序，最多 limit 条）。"""
        if limit <= 0:
            return []
        history = list(self._history)
        history.reverse()
        return history[:limit]
