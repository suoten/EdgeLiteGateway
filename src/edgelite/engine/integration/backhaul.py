"""EdgeLite v1.0 数据回传管理器 - 基于v1.0的EventBus(register_handler)接口"""

import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from edgelite.api.error_codes import IntegrationErrors

logger = logging.getLogger(__name__)


@dataclass
class RpcCommand:
    """RPC指令数据类"""
    method: str
    device_id: str
    params: dict[str, Any] = field(default_factory=dict)
    timeout: float = 10.0
    command_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])


@dataclass
class RpcResult:
    """RPC执行结果数据类"""
    command_id: str
    success: bool
    result: Any = None
    error: str | None = None
    elapsed_ms: float = 0.0


class BackhaulManager:
    def __init__(
        self,
        event_bus: Any,
        endpoint: Any,
        device_filter: list[str] | None = None,
        point_filter: list[str] | None = None,
        change_threshold: float = 0.0,
        rate_limit: float = 10.0,
        buffer_size: int = 1000,
    ):
        self._event_bus = event_bus
        self._endpoint = endpoint
        self._device_filter = set(device_filter or [])
        self._point_filter = set(point_filter or [])
        self._change_threshold = change_threshold
        self._rate_limit = rate_limit
        self._buffer_size = buffer_size
        self._buffer: deque[dict[str, Any]] = deque(maxlen=buffer_size)
        self._last_values: dict[str, float] = {}
        self._last_send_time: dict[str, float] = {}
        self._running = False
        self._rpc_history: deque[dict[str, Any]] = deque(maxlen=200)

    async def handle_rpc_command(
        self,
        command: RpcCommand,
        device_service: Any = None,
    ) -> RpcResult:
        """处理RPC反向控制指令，调用驱动写方法执行设备控制并记录审计日志。

        Args:
            command: RPC指令对象，包含method/device_id/params/timeout。
            device_service: 设备服务实例，用于执行写点操作。

        Returns:
            RpcResult: 执行结果，包含成功/失败状态和返回值。
        """
        start = time.time()
        try:
            if device_service is None:
                return RpcResult(
                    command_id=command.command_id,
                    success=False,
                    error=IntegrationErrors.RPC_DEVICE_SERVICE_UNAVAILABLE,
                    elapsed_ms=(time.time() - start) * 1000,
                )

            point = command.params.get("point", command.method)
            value = command.params.get("value")

            if value is None:
                return RpcResult(
                    command_id=command.command_id,
                    success=False,
                    error=IntegrationErrors.RPC_MISSING_VALUE,
                    elapsed_ms=(time.time() - start) * 1000,
                )

            success = await device_service.write_point(
                command.device_id, point, value
            )

            elapsed = (time.time() - start) * 1000
            result = RpcResult(
                command_id=command.command_id,
                success=success,
                result={"device_id": command.device_id, "point": point, "value": value} if success else None,
                error=None if success else IntegrationErrors.RPC_WRITE_FAILED,
                elapsed_ms=elapsed,
            )

            self._rpc_history.append({
                "command_id": command.command_id,
                "method": command.method,
                "device_id": command.device_id,
                "params": command.params,
                "success": success,
                "elapsed_ms": elapsed,
                "timestamp": time.time(),
            })

            logger.info(
                "RPC command executed: id=%s method=%s device=%s success=%s",
                command.command_id, command.method, command.device_id, success,
            )
            return result

        except Exception as e:
            elapsed = (time.time() - start) * 1000
            logger.error("RPC command failed: id=%s error=%s", command.command_id, e)
            return RpcResult(
                command_id=command.command_id,
                success=False,
                error=str(e),
                elapsed_ms=elapsed,
            )

    def get_rpc_history(self, limit: int = 50) -> list[dict[str, Any]]:
        """获取RPC执行历史记录。

        Args:
            limit: 返回记录数上限。

        Returns:
            按时间倒序排列的RPC执行历史列表。
        """
        records = list(self._rpc_history)
        records.reverse()
        return records[:limit]

    async def start(self) -> None:
        self._running = True
        if self._event_bus:
            self._event_bus.register_handler("PointUpdateEvent", self._on_point_update)
            self._event_bus.register_handler("DeviceStatusEvent", self._on_device_status)
            self._event_bus.register_handler("AlarmEvent", self._on_alarm)
        logger.info("BackhaulManager started")

    async def stop(self) -> None:
        self._running = False
        if self._event_bus:
            self._event_bus.unregister_handler("PointUpdateEvent", self._on_point_update)
            self._event_bus.unregister_handler("DeviceStatusEvent", self._on_device_status)
            self._event_bus.unregister_handler("AlarmEvent", self._on_alarm)
        logger.info("BackhaulManager stopped")

    async def _on_point_update(self, event: Any) -> None:
        device_id = getattr(event, "device_id", "")
        point_name = getattr(event, "point_name", "")
        value = getattr(event, "value", 0.0)

        if self._device_filter and device_id not in self._device_filter:
            return
        if self._point_filter and point_name not in self._point_filter:
            return

        key = f"{device_id}.{point_name}"
        if self._change_threshold > 0:
            last = self._last_values.get(key)
            if last is not None and abs(value - last) < self._change_threshold:
                return
            self._last_values[key] = value

        now = time.time()
        min_interval = 1.0 / self._rate_limit if self._rate_limit > 0 else 0
        last_send = self._last_send_time.get(device_id, 0)
        if now - last_send < min_interval:
            return
        self._last_send_time[device_id] = now

        await self._send_or_buffer(
            {
                "type": "point_data",
                "timestamp": now,
                "payload": {
                    "device_id": device_id,
                    "point_name": point_name,
                    "value": value,
                    "quality": getattr(event, "quality", "good"),
                },
            }
        )

    async def _on_device_status(self, event: Any) -> None:
        await self._send_or_buffer(
            {
                "type": "device_status_changed",
                "timestamp": time.time(),
                "payload": {
                    "device_id": getattr(event, "device_id", ""),
                    "new_status": getattr(event, "new_status", ""),
                    "old_status": getattr(event, "old_status", ""),
                },
            }
        )

    async def _on_alarm(self, event: Any) -> None:
        action = getattr(event, "action", "firing")
        await self._send_or_buffer(
            {
                "type": "alarm_fired" if action == "firing" else "alarm_recovered",
                "timestamp": time.time(),
                "payload": {
                    "alarm_id": getattr(event, "alarm_id", ""),
                    "rule_id": getattr(event, "rule_id", ""),
                    "device_id": getattr(event, "device_id", ""),
                    "severity": getattr(event, "severity", ""),
                    "action": action,
                },
            }
        )

    async def _send_or_buffer(self, message: dict[str, Any]) -> None:
        if self._endpoint and self._endpoint.has_connections:
            try:  # FIXED: 原问题-broadcast无try-catch，广播失败会中断事件处理
                sent = await self._endpoint.broadcast(message)
                if sent > 0:
                    return
            except Exception as e:
                logger.error("Broadcast message failed: %s", e)
        self._buffer.append(message)

    async def flush_buffer(self, max_retries: int = 3) -> int:
        """将缓冲区中的消息flush到所有连接。

        修复逻辑：连续失败 max_retries 次后丢弃当前消息，但重置计数器
        让下一条消息有机会被发送，避免连续失败后丢弃所有剩余消息。
        """
        if not self._buffer or not self._endpoint or not self._endpoint.has_connections:
            return 0
        count = 0
        consecutive_failures = 0
        while self._buffer:
            msg = self._buffer.popleft()
            try:
                sent = await self._endpoint.broadcast(msg)
                if sent > 0:
                    count += 1
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                    if consecutive_failures >= max_retries:
                        # 连续失败超过阈值，丢弃当前消息，重置计数器
                        logger.warning("Backhaul flush: message dropped after %d consecutive failures (buffer=%d)", max_retries, len(self._buffer))
                        consecutive_failures = 0
                    else:
                        self._buffer.appendleft(msg)
            except Exception as e:
                consecutive_failures += 1
                if consecutive_failures >= max_retries:
                    logger.warning("Backhaul flush: message dropped after %d consecutive exceptions: %s (buffer=%d)", max_retries, e, len(self._buffer))
                    consecutive_failures = 0
                else:
                    self._buffer.appendleft(msg)
        return count
