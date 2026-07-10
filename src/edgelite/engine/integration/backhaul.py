"""EdgeLite v1.0 数据回传管理器 - 基于v1.0的EventBus(register_handler)接口"""

import asyncio
import contextlib
import logging
import random
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from edgelite.api.error_codes import IntegrationErrors
from edgelite.storage.offline_queue import OfflineQueue

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
        offline_queue: OfflineQueue | None = None,
        resend_interval: float = 30.0,
    ):
        self._event_bus = event_bus
        self._endpoint = endpoint
        self._device_filter = set(device_filter or [])
        self._point_filter = set(point_filter or [])
        self._change_threshold = change_threshold
        self._rate_limit = rate_limit
        self._buffer_size = buffer_size
        # FIXED(一般): 原问题-_buffer从未被_send_or_buffer写入（改用_fallback_buffer），
        # flush_buffer引用_buffer导致永远无数据可刷新。修复-移除死代码_buffer，
        # flush_buffer改为使用_fallback_buffer
        self._last_values: dict[str, float] = {}
        self._last_send_time: dict[str, float] = {}
        # FIXED-P0: 添加锁保护_last_values和_last_send_time的并发访问，
        # 防止EventBus重试机制导致同一事件handler并发执行造成状态不一致
        self._state_lock = asyncio.Lock()
        # FIXED-P0: 离线队列入队失败时的内存兜底队列，防止数据丢失
        self._fallback_buffer: deque[dict[str, Any]] = deque(maxlen=10000)
        # FIXED(严重): _fallback_buffer 的并发读写保护锁
        # _send_or_buffer 写入与 flush_buffer 的 popleft/appendleft 并发会导致 deque 状态竞态
        self._fallback_lock = asyncio.Lock()
        self._running = False
        self._rpc_history: deque[dict[str, Any]] = deque(maxlen=200)
        self._offline_queue = offline_queue or OfflineQueue()
        self._resend_interval = resend_interval
        self._resend_task: asyncio.Task | None = None

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

            # SEC-FIX: RPC 反向控制写入前必须校验写保护策略
            # 通过 device_service 获取 driver 实例并调用 check_write_allowed
            # 同时构造 user 上下文，使驱动层 RBAC 与审计能识别 RPC 来源
            rpc_user = {
                "username": f"rpc:{command.command_id}",
                "role": "operator",
                "source": "rpc",
                "command_id": command.command_id,
            }
            try:
                driver_instance = await device_service.get_driver_instance(command.device_id)
                if driver_instance is not None and hasattr(driver_instance, "check_write_allowed"):
                    allowed = driver_instance.check_write_allowed(command.device_id, point)
                    if not allowed:
                        elapsed = (time.time() - start) * 1000
                        logger.warning(
                            "RPC command blocked by write-protection policy: id=%s device=%s point=%s",
                            command.command_id,
                            command.device_id,
                            point,
                        )
                        async with self._state_lock:
                            self._rpc_history.append(
                                {
                                    "command_id": command.command_id,
                                    "method": command.method,
                                    "device_id": command.device_id,
                                    "params": command.params,
                                    "success": False,
                                    "elapsed_ms": elapsed,
                                    "timestamp": time.time(),
                                    "error": "write_protection_blocked",
                                }
                            )
                        return RpcResult(
                            command_id=command.command_id,
                            success=False,
                            error="Write blocked by write-protection policy",
                            elapsed_ms=elapsed,
                        )
            except Exception as check_e:
                elapsed = (time.time() - start) * 1000
                logger.warning(
                    "RPC check_write_allowed raised: id=%s error=%s",
                    command.command_id,
                    check_e,
                )
                return RpcResult(
                    command_id=command.command_id,
                    success=False,
                    error=f"check_write_allowed failed: {check_e}",
                    elapsed_ms=elapsed,
                )

            success = await device_service.write_point(command.device_id, point, value, user=rpc_user)

            elapsed = (time.time() - start) * 1000
            result = RpcResult(
                command_id=command.command_id,
                success=success,
                result={"device_id": command.device_id, "point": point, "value": value} if success else None,
                error=None if success else IntegrationErrors.RPC_WRITE_FAILED,
                elapsed_ms=elapsed,
            )

            # FIXED(一般): 使用_state_lock保护_rpc_history并发写入
            async with self._state_lock:
                self._rpc_history.append(
                    {
                        "command_id": command.command_id,
                        "method": command.method,
                        "device_id": command.device_id,
                        "params": command.params,
                        "success": success,
                        "elapsed_ms": elapsed,
                        "timestamp": time.time(),
                    }
                )

            logger.info(
                "RPC command executed: id=%s method=%s device=%s success=%s",
                command.command_id,
                command.method,
                command.device_id,
                success,
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

    async def get_rpc_history(self, limit: int = 50) -> list[dict[str, Any]]:
        """获取RPC执行历史记录。

        Args:
            limit: 返回记录数上限。

        Returns:
            按时间倒序排列的RPC执行历史列表。
        """
        # FIXED(一般): 原问题-_rpc_history与handle_rpc_command并发修改竞态;
        # 修复-使用_state_lock保护读取
        async with self._state_lock:
            records = list(self._rpc_history)
        records.reverse()
        return records[:limit]

    async def start(self) -> None:
        self._running = True
        if self._event_bus:
            self._event_bus.register_handler("PointUpdateEvent", self._on_point_update)
            self._event_bus.register_handler("DeviceStatusEvent", self._on_device_status)
            self._event_bus.register_handler("AlarmEvent", self._on_alarm)
        self._resend_task = asyncio.create_task(self._resend_loop())
        logger.info("BackhaulManager started")

    async def stop(self) -> None:
        self._running = False
        if self._resend_task is not None:
            self._resend_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._resend_task
            self._resend_task = None
        if self._event_bus:
            self._event_bus.unregister_handler("PointUpdateEvent", self._on_point_update)
            self._event_bus.unregister_handler("DeviceStatusEvent", self._on_device_status)
            self._event_bus.unregister_handler("AlarmEvent", self._on_alarm)
        await self._offline_queue.close()
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
        # FIXED-P0: 加锁保护_last_values和_last_send_time的读-改-写操作
        async with self._state_lock:
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
        # 网络不可达时，将数据存入离线队列（断点续传）
        msg_type = message.get("type", "unknown")
        # BUG-018: 离线队列写入失败时记录数据丢失告警
        try:
            await self._offline_queue.enqueue(msg_type, message)
        except Exception as e:
            # FIXED-P0: 原问题-离线队列入队失败时数据直接丢失，无降级方案。
            # 改为写入内存兜底队列_fallback_buffer，下次重试时从_fallback_buffer恢复。
            # FIXED(严重): _fallback_buffer 读写加 _fallback_lock 保护，避免与 flush_buffer 并发导致 deque 竞态
            async with self._fallback_lock:
                logger.error(
                    "Backhaul: offline queue enqueue failed, writing to fallback buffer. type=%s fallback_size=%d error=%s",  # noqa: E501
                    msg_type,
                    len(self._fallback_buffer),
                    e,
                )
                try:
                    self._fallback_buffer.append({"type": msg_type, "message": message})
                except Exception as fallback_e:
                    logger.error(
                        "Backhaul: fallback buffer also failed, data lost. type=%s error=%s",
                        msg_type,
                        fallback_e,
                    )
        # FIXED-P2: 原问题-同时写入离线队列和内存缓冲区导致重复存储；改为只写入离线队列

    async def flush_buffer(self, max_retries: int = 3) -> int:
        """将缓冲区中的消息flush到所有连接。

        修复逻辑：连续失败 max_retries 次后丢弃当前消息，但重置计数器
        让下一条消息有机会被发送，避免连续失败后丢弃所有剩余消息。
        """
        # FIXED(一般): 原问题-引用已移除的_buffer; 修复-使用_fallback_buffer
        if not self._fallback_buffer or not self._endpoint or not self._endpoint.has_connections:
            return 0
        count = 0
        consecutive_failures = 0
        # FIXED(严重): _fallback_buffer 并发读写加锁保护
        # 采用锁内取消息、锁外广播的策略：broadcast 是 IO 操作，持锁期间会阻塞 _send_or_buffer 写入
        while True:
            async with self._fallback_lock:
                if not self._fallback_buffer:
                    break
                msg = self._fallback_buffer.popleft()
            try:
                sent = await self._endpoint.broadcast(msg)
                if sent > 0:
                    count += 1
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                    if consecutive_failures >= max_retries:
                        logger.warning(
                            "Backhaul flush: message dropped after %d consecutive failures (buffer=%d)",
                            max_retries,
                            len(self._fallback_buffer),
                        )
                        consecutive_failures = 0
                    else:
                        async with self._fallback_lock:
                            self._fallback_buffer.appendleft(msg)
            except Exception as e:
                consecutive_failures += 1
                if consecutive_failures >= max_retries:
                    logger.warning(
                        "Backhaul flush: message dropped after %d consecutive exceptions: %s (buffer=%d)",
                        max_retries,
                        e,
                        len(self._fallback_buffer),
                    )
                    consecutive_failures = 0
                else:
                    async with self._fallback_lock:
                        self._fallback_buffer.appendleft(msg)
        return count

    async def _resend_loop(self) -> None:
        """定期尝试补传离线队列中的数据至云端"""
        consecutive_failures = 0  # FIXED-P3: 原问题-固定间隔重试无退避，添加指数退避
        while self._running:
            try:
                interval = self._resend_interval * min(2**consecutive_failures, 10)
                interval *= 0.5 + random.random() * 0.5  # FIXED-P3: 退避抖动
                await asyncio.sleep(interval)
                if not self._running:
                    break
                # R7-S-03: 清理过期和超过最大重试次数的数据必须在连接检查之前执行，
                # 否则离线时 purge_expired/purge_max_retries 永远不会执行，离线队列无限增长
                await self._offline_queue.purge_expired()
                await self._offline_queue.purge_max_retries()
                # 仅在有连接时才尝试补传
                if not self._endpoint or not self._endpoint.has_connections:
                    continue
                await self._resend_pending()
                consecutive_failures = 0  # 成功后重置
            except asyncio.CancelledError:
                break
            except Exception as e:
                consecutive_failures += 1  # FIXED-P3: 失败时递增退避计数
                logger.error("[backhaul] Resend loop error: %s", e)

    async def _resend_pending(self) -> int:
        """补传离线队列中的待发送数据"""
        batch = await self._offline_queue.dequeue_batch()
        if not batch:
            return 0
        success_ids: list[int] = []
        fail_ids: list[int] = []
        last_error = ""
        for item in batch:
            message = item["payload"]
            try:
                if self._endpoint and self._endpoint.has_connections:
                    sent = await self._endpoint.broadcast(message)
                    if sent > 0:
                        success_ids.append(item["id"])
                        continue
                # 无连接或发送0，视为失败
                fail_ids.append(item["id"])
                last_error = "no connections or broadcast returned 0"
            except Exception as e:
                fail_ids.append(item["id"])
                last_error = str(e)

        if success_ids:
            await self._offline_queue.acknowledge(success_ids)
        if fail_ids:
            await self._offline_queue.increment_retry(fail_ids, last_error)

        if success_ids:
            logger.info("[backhaul] Resent %d offline messages, %d still pending", len(success_ids), len(fail_ids))
        return len(success_ids)
