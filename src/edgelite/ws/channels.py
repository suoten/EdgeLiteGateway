"""WebSocket频道定义与事件处理"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Callable
from typing import Any

from edgelite.engine.event_bus import AlarmEvent, DeviceStatusEvent, EventBus, PointUpdateEvent
from edgelite.ws.manager import ConnectionManager

logger = logging.getLogger(__name__)

# 敏感字段名关键词(小写子串匹配), 命中即对整个值做掩码
_SENSITIVE_FIELD_KEYWORDS = ("token", "password", "passwd", "api_key", "apikey", "secret", "credential")
# 匹配 URL 中嵌入的凭据: scheme://user:pass@host
_URL_CRED_RE = re.compile(r"(^[a-zA-Z][a-zA-Z0-9+.\-]*://)([^:/@\s]+):([^:/@\s]+)@")


def _mask_url_credentials(value: str) -> str:
    """掩码 URL 中嵌入的凭据(user:pass@host → ***:***@host), 其余部分保留"""
    return _URL_CRED_RE.sub(r"\1***:***@", value)


def _mask_sensitive_fields(data: Any) -> Any:
    """递归对字典/列表中的敏感字段进行掩码, 防止凭据经 integration 频道广播泄漏。

    - 命中敏感字段名(token/password/api_key/secret/credential 等)的值整体替换为 "***"
    - 其余字符串值掩码其中嵌入的 URL 凭据(若有)
    """
    if isinstance(data, dict):
        masked: dict = {}
        for k, v in data.items():
            key_lower = str(k).lower()
            if any(s in key_lower for s in _SENSITIVE_FIELD_KEYWORDS):
                # 已知敏感字段: 整体掩码
                masked[k] = "***"
            elif isinstance(v, str):
                masked[k] = _mask_url_credentials(v)
            else:
                masked[k] = _mask_sensitive_fields(v)
        return masked
    if isinstance(data, list):
        return [_mask_sensitive_fields(item) for item in data]
    if isinstance(data, str):
        return _mask_url_credentials(data)
    return data


class WebSocketChannels:
    """WebSocket频道管理，订阅EventBus并广播到前端"""

    def __init__(self, event_bus: EventBus, manager: ConnectionManager):
        self._event_bus = event_bus
        self._manager = manager
        self._tasks: list[asyncio.Task] = []
        # FIXED-P1: 记录订阅名称，stop()时取消订阅防止内存泄漏
        self._subscription_names: list[str] = []

    async def start(self) -> None:
        """启动所有频道的EventBus订阅"""
        # realtime频道：测点值更新
        realtime_queue = await self._event_bus.subscribe("ws_realtime")
        self._subscription_names.append("ws_realtime")
        self._tasks.append(
            asyncio.create_task(
                self._channel_loop("realtime", realtime_queue, self._format_point_update),
                name="ws-realtime",
            )
        )

        # alarm频道：告警事件
        alarm_queue = await self._event_bus.subscribe("ws_alarm")
        self._subscription_names.append("ws_alarm")
        self._tasks.append(
            asyncio.create_task(
                self._channel_loop("alarm", alarm_queue, self._format_alarm),
                name="ws-alarm",
            )
        )

        # device频道：设备状态变更
        device_queue = await self._event_bus.subscribe("ws_device")
        self._subscription_names.append("ws_device")
        self._tasks.append(
            asyncio.create_task(
                self._channel_loop("device", device_queue, self._format_device_status),
                name="ws-device",
            )
        )

        # integration频道：北向集成事件
        # R5-S-06 修复(严重): integration 频道包含北向适配器状态、外部平台通信细节等敏感信息，
        # 仅 admin/operator 角色可订阅，普通用户不应接收。
        integration_queue = await self._event_bus.subscribe("ws_integration")
        self._subscription_names.append("ws_integration")
        self._tasks.append(
            asyncio.create_task(
                self._channel_loop(
                    "integration", integration_queue, self._format_integration,
                    filter_fn=lambda meta: meta.get("role") in ("admin", "operator"),
                ),
                name="ws-integration",
            )
        )

        logger.info("WebSocket channels started")

    async def stop(self) -> None:
        """停止所有频道"""
        for task in self._tasks:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._tasks.clear()
        # FIXED-P1: 原问题-stop()未取消EventBus订阅，导致队列引用无法释放，内存泄漏
        for name in self._subscription_names:
            try:
                await self._event_bus.unsubscribe(name)
            except Exception as e:
                logger.warning("Failed to unsubscribe %s: %s", name, e)
        self._subscription_names.clear()
        logger.info("WebSocket channels stopped")

    async def _channel_loop(
        self,
        channel: str,
        queue: asyncio.Queue,
        formatter,
        filter_fn: Callable[[dict[str, Any]], bool] | None = None,
    ) -> None:
        """频道事件循环

        FIXED-P1: 添加背压控制 - 当队列积压超过阈值时记录警告
        FIXED-P2: 异常后添加短暂延迟，防止formatter持续异常导致快速空转消耗CPU
        R5-S-06: 新增 filter_fn 参数，支持按连接用户身份过滤广播消息。
        """
        while True:
            try:
                event = await queue.get()
                # FIXED-P1: 背压检测 - 队列积压过多时记录警告，帮助发现广播瓶颈
                queue_size = queue.qsize()
                if queue_size > 100:
                    logger.warning(
                        "WebSocket channel %s queue backlog: %d events pending, "
                        "broadcast may be too slow", channel, queue_size
                    )
                data = formatter(event)
                if data:
                    await self._manager.broadcast(channel, data, filter_fn=filter_fn)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("WebSocket channel error: %s - %s", channel, e)
                # FIXED-P2: 异常后短暂延迟，防止持续异常导致CPU空转
                await asyncio.sleep(0.1)

    @staticmethod
    def _format_point_update(event) -> dict | None:
        if not isinstance(event, PointUpdateEvent):
            return None
        return {
            "type": "point_update",
            "device_id": event.device_id,
            "point_name": event.point_name,
            "value": event.value,
            "quality": event.quality,
            "timestamp": event.timestamp,
        }

    @staticmethod
    def _format_alarm(event) -> dict | None:
        if not isinstance(event, AlarmEvent):
            return None
        return {
            "type": "alarm",
            "alarm_id": event.alarm_id,
            "rule_id": event.rule_id,
            "rule_name": event.rule_name,
            "device_id": event.device_id,
            "device_name": event.device_name,
            "severity": event.severity,
            "action": event.action,
            "timestamp": event.timestamp,
        }

    @staticmethod
    def _format_device_status(event) -> dict | None:
        if not isinstance(event, DeviceStatusEvent):
            return None
        return {
            "type": "device_status",
            "device_id": event.device_id,
            "old_status": event.old_status,
            "new_status": event.new_status,
            "timestamp": event.timestamp,
        }

    @staticmethod
    def _format_integration(event) -> dict | None:
        # FIXED: 过滤非集成事件类型，避免 AlarmEvent/DeviceStatusEvent 等泄漏到 integration 频道
        # FIXED-P2: 原问题-函数内重复导入已模块级导入的类，移除冗余导入
        if isinstance(event, (AlarmEvent, DeviceStatusEvent, PointUpdateEvent)):
            return None
        if isinstance(event, dict):
            # FIXED(一般): 原问题-对dict类型事件直接返回原始字典广播, 无字段级脱敏;
            # 修复-经 _mask_sensitive_fields 脱敏后再广播
            return _mask_sensitive_fields(event)
        if hasattr(event, "model_dump"):
            return _mask_sensitive_fields(event.model_dump())
        return None
