"""EdgeLite v1.0 消息分发器"""

import logging
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)


class MessageDispatcher:
    def __init__(self):
        self._handlers: dict[str, Callable] = {}
        self._services: dict[str, Any] = {}

    def register_handler(self, msg_type: str, handler: Callable) -> None:
        self._handlers[msg_type] = handler

    def register_service(self, name: str, service: Any) -> None:
        self._services[name] = service

    async def dispatch(self, msg_type: str, payload: dict[str, Any], session_id: str = "") -> dict[str, Any] | None:
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
            return {"type": "heartbeat_ack", "timestamp": __import__("time").time()}
        elif msg_type == "handshake":
            return {"type": "handshake_ack", "version": "1.0"}

        return {"ok": False, "error": f"Unknown message type: {msg_type}"}

    async def _handle_push_device(self, payload: dict[str, Any]) -> dict[str, Any]:
        device_service = self._services.get("device_service")
        if not device_service:
            return {"ok": False, "error": "Device service not available"}
        try:
            await device_service.create_device(payload)
            return {"ok": True, "device_id": payload.get("device_id", "")}
        except Exception as e:
            if "already exists" in str(e).lower() or "409" in str(e):
                try:
                    device_id = payload.get("device_id", "")
                    update_data = {k: v for k, v in payload.items() if k != "device_id"}
                    await device_service.update_device(device_id, update_data)
                    return {"ok": True, "updated": True, "device_id": device_id}
                except Exception as ue:
                    return {"ok": False, "error": f"Update failed: {ue}"}
            return {"ok": False, "error": str(e)}

    async def _handle_delete_device(self, payload: dict[str, Any]) -> dict[str, Any]:
        device_service = self._services.get("device_service")
        if not device_service:
            return {"ok": False, "error": "Device service not available"}
        try:
            await device_service.delete_device(payload.get("device_id", ""))
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def _handle_device_control(self, payload: dict[str, Any]) -> dict[str, Any]:
        device_service = self._services.get("device_service")
        if not device_service:
            return {"ok": False, "error": "Device service not available"}
        device_id = payload.get("device_id", "")
        action = payload.get("action", "")
        try:
            if action == "start_collect":
                await device_service.start_collect(device_id)
            elif action == "stop_collect":
                await device_service.stop_collect(device_id)
            else:
                return {"ok": False, "error": f"Unknown action: {action}"}
            return {"ok": True, "device_id": device_id, "action": action}
        except Exception as e:
            return {"ok": False, "error": str(e)}
