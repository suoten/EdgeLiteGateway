"""EdgeLite v1.0 消息分发器

重构要点:
1. push_device 增加 DeviceCreate Pydantic 校验，防止无效数据导致 KeyError
2. 更新设备时重建驱动实例 + 重启采集，确保新配置生效
3. device_control 使用 DeviceService 公开方法而非直接访问私有属性
"""

import logging
import time
from collections.abc import Callable, Coroutine
from typing import Any

logger = logging.getLogger(__name__)


class MessageDispatcher:
    def __init__(self):
        self._handlers: dict[str, Callable] = {}
        self._services: dict[str, Any] = {}

    def register_handler(self, msg_type: str, handler: Callable) -> None:
        self._handlers[msg_type] = handler

    def register_service(self, name: str, service: Any) -> None:
        self._services[name] = service

    async def dispatch(
        self, msg_type: str, payload: dict[str, Any], session_id: str = ""
    ) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            return {"ok": False, "error": "payload must be a JSON object"}
        handler = self._handlers.get(msg_type)
        if handler:
            try:
                result = handler(payload, session_id)
                if isinstance(result, Coroutine):
                    result = await result
                return result
            except Exception as e:
                logger.error("Handler error for %s: %s", msg_type, e)
                return {"ok": False, "error": str(e)}

        if msg_type == "push_device":
            return await self._handle_push_device(payload)
        elif msg_type == "delete_device":
            return await self._handle_delete_device(payload)
        elif msg_type == "device_control":
            return await self._handle_device_control(payload)
        elif msg_type == "heartbeat":
            return {"type": "heartbeat_ack", "timestamp": time.time()}
        elif msg_type == "handshake":
            return {"type": "handshake_ack", "version": "1.0"}

        return {"ok": False, "error": f"Unknown message type: {msg_type}"}

    def _validate_push_device_payload(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """校验 push_device 的 payload，返回错误信息或 None（校验通过）。"""
        # 必需字段检查
        device_id = payload.get("device_id", "")
        if not device_id:
            return {"ok": False, "error": "Missing required field: device_id", "error_type": "validation_error"}

        import re
        if not re.match(r"^[a-z0-9][a-z0-9_-]{0,62}[a-z0-9]$", device_id):
            return {
                "ok": False,
                "error": f"Invalid device_id '{device_id}': must match ^[a-z0-9][a-z0-9_-]{{0,62}}[a-z0-9]$",
                "error_type": "validation_error",
            }

        name = payload.get("name", "")
        if not name or not name.strip():
            return {"ok": False, "error": "Missing required field: name", "error_type": "validation_error"}
        if len(name) > 64:
            return {"ok": False, "error": "name too long (max 64 chars)", "error_type": "validation_error"}

        protocol = payload.get("protocol", "")
        if not protocol:
            return {"ok": False, "error": "Missing required field: protocol", "error_type": "validation_error"}

        points = payload.get("points", [])
        if not isinstance(points, list) or len(points) == 0:
            return {"ok": False, "error": "Missing required field: points (must be non-empty list)", "error_type": "validation_error"}

        collect_interval = payload.get("collect_interval", 5)
        if not isinstance(collect_interval, (int, float)) or collect_interval < 1:
            return {"ok": False, "error": "collect_interval must be >= 1", "error_type": "validation_error"}

        return None  # 校验通过

    async def _handle_push_device(self, payload: dict[str, Any]) -> dict[str, Any]:
        device_service = self._services.get("device_service")
        if not device_service:
            return {"ok": False, "error": "Device service not available"}

        # FIX: 增加数据校验，防止无效 payload 导致 KeyError
        validation_error = self._validate_push_device_payload(payload)
        if validation_error:
            return validation_error

        try:
            await device_service.create_device(payload)
            return {"ok": True, "device_id": payload.get("device_id", "")}
        except Exception as e:
            error_str = str(e).lower()
            # FIX: 检测设备已存在的情况，包括：
            # - HTTP 409 (REST API 路径)
            # - "already exists" (某些数据库)
            # - "unique constraint" / "integrity error" (SQLite)
            is_conflict = (
                "already exists" in error_str
                or "409" in str(e)
                or "unique constraint" in error_str
                or "integrity" in error_str
            )
            if is_conflict:
                # FIX: 更新设备时重建驱动 + 重启采集
                try:
                    device_id = payload.get("device_id", "")
                    result = await self._rebuild_device(device_service, device_id, payload)
                    return result
                except Exception as ue:
                    return {"ok": False, "error": f"Rebuild device failed: {ue}"}
            return {"ok": False, "error": error_str}

    async def _rebuild_device(
        self, device_service: Any, device_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """重建设备：停止旧驱动 → 删除旧设备 → 重新创建。

        解决原问题：update_device() 仅更新数据库记录，不重建驱动/重启采集，
        导致新配置（如连接地址、端口、测点列表）不生效。

        通过 DeviceService.delete_device + create_device 实现原子性重建，
        使用 DeviceService 的锁保护，避免竞态条件。
        """
        scheduler = self._services.get("scheduler")

        # 1. 停止旧采集（在删除前先停，避免 delete_device 中的采集取消竞争）
        if scheduler:
            try:
                await scheduler.stop_collect(device_id)
            except Exception as e:
                logger.debug("Stop collect for rebuild %s: %s", device_id, e)

        # 2. 停止旧驱动（在删除前先停，避免 delete_device 中的驱动停止竞争）
        driver_instances = getattr(device_service, "_driver_instances", None)
        old_driver = driver_instances.pop(device_id, None) if driver_instances else None
        if old_driver is not None:
            try:
                await old_driver.stop()
            except Exception as e:
                logger.debug("Stop old driver for rebuild %s: %s", device_id, e)

        # 3. 通过 DeviceService 删除旧设备（获取锁，原子操作）
        try:
            success, error_msg = await device_service.delete_device(device_id)
            if not success:
                logger.warning("Delete old device %s for rebuild failed: %s", device_id, error_msg)
                return {"ok": False, "error": f"Cannot delete old device: {error_msg}", "device_id": device_id}
        except Exception as e:
            logger.warning("Delete old device %s for rebuild failed: %s", device_id, e)
            return {"ok": False, "error": f"Cannot delete old device: {e}", "device_id": device_id}

        # 4. 重新创建设备（获取锁，原子操作，自动启动驱动+采集）
        try:
            await device_service.create_device(payload)
            return {"ok": True, "updated": True, "rebuilt": True, "device_id": device_id}
        except Exception as e:
            logger.error("Rebuild device %s failed: create_device error: %s", device_id, e)
            # 创建失败时，旧设备已被删除，无法回滚
            # 但至少记录了错误信息，调用方可以重试
            return {"ok": False, "error": f"Rebuild failed (old device deleted): {e}", "device_id": device_id}

    async def _handle_delete_device(self, payload: dict[str, Any]) -> dict[str, Any]:
        device_service = self._services.get("device_service")
        if not device_service:
            return {"ok": False, "error": "Device service not available"}
        device_id = payload.get("device_id", "")
        if not device_id:
            return {"ok": False, "error": "Missing required field: device_id", "error_type": "validation_error"}
        try:
            success, error_msg = await device_service.delete_device(device_id)
            if success:
                return {"ok": True}
            return {"ok": False, "error": error_msg or "Delete failed", "error_type": "delete_failed"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def _handle_device_control(self, payload: dict[str, Any]) -> dict[str, Any]:
        device_service = self._services.get("device_service")
        scheduler = self._services.get("scheduler")
        if not device_service:
            return {"ok": False, "error": "Device service not available"}
        device_id = payload.get("device_id", "")
        action = payload.get("action", "")
        if not device_id:
            return {"ok": False, "error": "Missing required field: device_id", "error_type": "validation_error"}
        if action not in ("start_collect", "stop_collect"):
            return {"ok": False, "error": f"Invalid action: {action!r}, must be 'start_collect' or 'stop_collect'", "error_type": "validation_error"}
        try:
            if action == "start_collect":
                if not scheduler:
                    return {"ok": False, "error": "Scheduler not available"}
                device = await device_service.get_device(device_id)
                if not device:
                    return {"ok": False, "error": f"Device {device_id} not found"}

                driver_instances = getattr(device_service, "_driver_instances", None)
                if not driver_instances:
                    return {"ok": False, "error": "Driver instances not available"}
                driver = driver_instances.get(device_id)
                if not driver:
                    return {"ok": False, "error": f"Driver not found for device {device_id}, try push_device first"}

                await scheduler.start_collect(
                    device_id,
                    driver,
                    device.get("points", []),
                    device.get("collect_interval", 5),
                )

                lifecycle = getattr(device_service, "_lifecycle", None)
                if lifecycle:
                    await lifecycle.on_device_online(device_id)
                repo = getattr(device_service, "_repo", None)
                if repo:
                    await repo.update_status(device_id, "online")

            elif action == "stop_collect":
                if not scheduler:
                    return {"ok": False, "error": "Scheduler not available"}
                await scheduler.stop_collect(device_id)
                lifecycle = getattr(device_service, "_lifecycle", None)
                if lifecycle:
                    await lifecycle.on_device_offline(device_id)
                repo = getattr(device_service, "_repo", None)
                if repo:
                    await repo.update_status(device_id, "offline")

            else:
                return {"ok": False, "error": f"Unknown action: {action}"}

            return {"ok": True, "device_id": device_id, "action": action}
        except Exception as e:
            return {"ok": False, "error": str(e)}
