"""Device Shadow Service — 设备影子服务。

维护设备状态的本地副本（reported/desired），支持：
- 设备上报状态 → 更新 reported state
- 应用层下发 desired state → 通过事件总线通知设备同步

参考 AWS IoT Thing Shadow / Azure IoT Device Twin 模型。
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


class ShadowService:
    """设备影子服务。"""

    def __init__(self) -> None:
        self._device_service: Any = None
        self._event_bus: Any = None
        self._audit_service: Any = None
        self._shadows: dict[str, dict[str, Any]] = {}  # device_id -> {"reported": {}, "desired": {}, "ts": 0}
        self._started = False

    def set_device_service(self, device_service: Any) -> None:
        self._device_service = device_service

    def set_event_bus(self, event_bus: Any) -> None:
        self._event_bus = event_bus

    def set_audit_service(self, audit_service: Any) -> None:
        self._audit_service = audit_service

    async def start(self) -> None:
        """启动影子服务。"""
        self._started = True
        logger.info("Shadow service started")

    async def stop(self) -> None:
        """停止影子服务。"""
        self._started = False
        logger.info("Shadow service stopped")

    def get_shadow(self, device_id: str) -> dict[str, Any] | None:
        """获取设备影子状态。"""
        return self._shadows.get(device_id)

    def update_reported(self, device_id: str, state: dict[str, Any]) -> None:
        """更新设备 reported 状态（设备上报）。"""
        shadow = self._shadows.setdefault(device_id, {"reported": {}, "desired": {}, "ts": 0})
        shadow["reported"].update(state)
        shadow["ts"] = time.time()

    def update_desired(self, device_id: str, state: dict[str, Any]) -> None:
        """更新设备 desired 状态（应用层下发）。"""
        shadow = self._shadows.setdefault(device_id, {"reported": {}, "desired": {}, "ts": 0})
        shadow["desired"].update(state)
        shadow["ts"] = time.time()
        # desired 审计日志
        if self._audit_service:
            try:
                self._audit_service.log(
                    action="shadow_desired_update",
                    target=device_id,
                    detail=state,
                )
            except Exception as e:
                logger.debug("Audit log for shadow desired update failed: %s", e)

    def delete_shadow(self, device_id: str) -> bool:
        """删除设备影子。"""
        return self._shadows.pop(device_id, None) is not None
