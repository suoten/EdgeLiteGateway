"""OPC UA 设备固件升级（OTA）管理。

FIX-DRV-005: 文件曾损坏（未闭合 docstring 导致 SyntaxError），之后被恢复为"最小桩"，
但桩的接口与 OpcUaDriver 的实际调用不符：驱动用 ``package_id / version / firmware_url /
firmware_hash / firmware_size`` 构造 :class:`OtaPackage`（桩里没有这些字段 → TypeError），
并调用 ``start_ota / rollback_ota / get_progress / get_history`` 以及接收包对象的
``check_update``（桩里没有这些方法 → AttributeError，或返回 None 而接口要的是 dict）。

OPC UA 固件升级在社区版尚未实现：这里把接口补齐，未实现的操作返回
``{"ok": False, "error": ERR_OTA_NOT_SUPPORTED}``，而不是抛异常或谎报成功。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

ERR_OTA_NOT_SUPPORTED = "ERR_OTA_NOT_SUPPORTED"
_OTA_NOT_SUPPORTED_MESSAGE = "OTA is not supported for OPC UA devices in this edition"


@dataclass
class OtaPackage:
    """OTA firmware package metadata."""

    # 与 FINS/驱动调用方一致的包描述字段
    package_id: str = ""
    name: str = ""
    version: str = ""
    file_path: str = ""
    checksum: str = ""
    size: int = 0
    firmware_url: str = ""
    firmware_hash: str = ""
    firmware_size: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


def _not_supported(operation: str) -> dict[str, Any]:
    return {"ok": False, "error": ERR_OTA_NOT_SUPPORTED, "operation": operation, "message": _OTA_NOT_SUPPORTED_MESSAGE}


class OpcUaOtaManager:
    """Manages OTA firmware upgrades for OPC UA devices.

    接口与 OpcUaDriver 的调用方式一一对应：
    - ``await check_update(package) -> dict``：``{"update_available": False, ...}``
    - ``await start_ota(package, config_snapshot) -> dict``
    - ``await rollback_ota() -> dict``
    - ``get_progress() -> dict`` / ``get_history(limit) -> list[dict]``
    """

    def __init__(self) -> None:
        self._packages: dict[str, OtaPackage] = {}
        self._history: list[dict[str, Any]] = []
        self._progress: dict[str, Any] = {"status": "idle", "percent": 0.0, "version": None, "message": ""}

    def _record(self, action: str, package: OtaPackage | None, ok: bool, error: str | None = None) -> None:
        entry = {
            "action": action,
            "package_id": getattr(package, "package_id", ""),
            "version": getattr(package, "version", None),
            "ok": ok,
            "error": error,
        }
        self._history.append(entry)
        del self._history[:-200]  # 最多保留 200 条

    # ── OpcUaDriver 使用的接口 ──────────────────────────────────────────

    async def check_update(self, package: OtaPackage | str) -> dict[str, Any]:
        """Check if a firmware update is available.

        驱动传入 :class:`OtaPackage`；兼容旧调用方式传入 device_id 字符串。
        """
        return {
            "update_available": False,
            "reason": "not_supported",
            "message": _OTA_NOT_SUPPORTED_MESSAGE,
        }

    async def start_ota(self, package: OtaPackage, config_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
        result = _not_supported("start")
        self._record("start", package, ok=False, error=ERR_OTA_NOT_SUPPORTED)
        return result

    async def rollback_ota(self) -> dict[str, Any]:
        result = _not_supported("rollback")
        self._record("rollback", None, ok=False, error=ERR_OTA_NOT_SUPPORTED)
        return result

    def get_progress(self) -> dict[str, Any]:
        return dict(self._progress)

    def get_history(self, limit: int = 20) -> list[dict[str, Any]]:
        return list(self._history[-max(0, limit) :]) if limit else []

    # ── 早期桩接口（保留以兼容既有调用） ────────────────────────────────

    async def download_package(self, url: str) -> OtaPackage:
        """Download a firmware package."""
        return OtaPackage()

    async def apply_update(self, device_id: str, package: OtaPackage) -> bool:
        """Apply a firmware update to a device."""
        # 社区版未实现：此前这里不做任何事却返回 True，调用方会误以为升级成功
        logger.warning("OTA update requested for device %s but OTA is not supported for OPC UA (stub)", device_id)
        return False

    async def rollback(self, device_id: str) -> bool:
        """Rollback the last firmware update."""
        logger.warning("OTA rollback requested for device %s but OTA is not supported for OPC UA (stub)", device_id)
        return False
