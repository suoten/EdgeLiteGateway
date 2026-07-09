"""视频接入驱动 + GB28181 协议支持"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from edgelite.drivers.base import DriverPlugin

logger = logging.getLogger(__name__)

_RECONNECT_BASE_DELAY = 2.0
_RECONNECT_MAX_DELAY = 60.0
_RECONNECT_MAX_ATTEMPTS = 5


class VideoDriver(DriverPlugin):
    plugin_name = "video"
    plugin_version = "1.0.1"
    supported_protocols = ["gb28181", "video"]

    config_schema = {
        "description": "GB28181 视频监控协议驱动，支持通过 PyGBSentry 接入视频流及 PTZ 云台控制",
        "fields": [
            {"name": "pygbsentry_url", "type": "string", "label": "PyGBSentry Address",
             "description": "PyGBSentry API address, e.g. http://127.0.0.1:8080", "default": "http://127.0.0.1:8080", "required": True},
            {"name": "username", "type": "string", "label": "Username",
             "description": "PyGBSentry login username", "default": "admin"},
            {"name": "password", "type": "string", "label": "Password",
             "description": "PyGBSentry login password", "secret": True},
            {"name": "reconnect_interval", "type": "float", "label": "Reconnect Interval (s)",
             "description": "重连间隔（秒），失败后指数退避", "default": 30.0},
        ],
    }

    def __init__(self):
        super().__init__()
        self._provider = None
        self._provider_config: dict = {}
        self._devices: dict[str, dict] = {}
        self._data_callback = None
        self._watchdog_task: asyncio.Task | None = None
        self._reconnect_count: int = 0
        self._reconnect_delay: float = _RECONNECT_BASE_DELAY
        self._provider_connected: bool = False

    async def start(self, config: dict) -> None:
        from edgelite.drivers.video.pygbsentry import PyGBSentryProvider

        self._provider_config = config
        self._provider = PyGBSentryProvider(config)
        await self._provider.connect()
        self._provider_connected = True
        self._running = True
        self._reconnect_count = 0
        self._reconnect_delay = _RECONNECT_BASE_DELAY
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())
        logger.info(
            "[video] GB28181驱动启动成功: %s",
            config.get("pygbsentry_url", "http://127.0.0.1:8080"),
        )

    async def stop(self) -> None:
        self._running = False
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watchdog_task
            self._watchdog_task = None
        if self._provider:
            await self._provider.close()
            self._provider = None
        self._provider_connected = False
        logger.info("[video] GB28181驱动已停止")

    async def add_device(self, device_id: str, config: dict, points: list[dict] | None = None) -> None:
        if points is None:
            points = []
        self._devices[device_id] = {
            "config": config,
            "points": {
                p.get("name", p.get("address", "")): p
                for p in points
                if p.get("name") or p.get("address")
            },
        }
        self._set_device_config(device_id, config)
        if self._provider:
            try:
                await self._provider.register_device(device_id, config)
                logger.info("[video] 视频设备注册成功: %s", device_id)
            except Exception as e:
                logger.warning("[video] 视频设备注册失败: %s - %s", device_id, e)

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if not self._provider:
            for p in points:
                result[p] = None
            return result
        try:
            status = await self._provider.get_device_status(device_id)
            if "status" in points:
                result["status"] = status.value if hasattr(status, "value") else str(status)
            if "stream_url" in points:
                url = await self._provider.get_stream_url(device_id, "1")
                result["stream_url"] = url or ""
        except Exception as e:
            logger.debug("[video] 读取测点失败: device=%s error=%s", device_id, e)
            for p in points:
                result[p] = None
        return result

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        if not self._provider:
            return False
        try:
            if point.startswith("ptz_"):
                action = point[4:]
                return await self._provider.ptz_control(
                    device_id,
                    "1",
                    action,
                    **( {"speed": value} if isinstance(value, (int, float)) else {} ),
                )
            return False
        except Exception as e:
            logger.warning("[video] PTZ控制失败: device=%s point=%s error=%s", device_id, point, e)
            return False

    async def discover_devices(self, config: dict) -> list[dict]:
        return []

    def on_data(self, callback) -> None:
        self._data_callback = callback

    def is_device_connected(self, device_id: str) -> bool:
        return self._running and self._provider_connected and self._provider is not None

    async def health_check(self, device_id: str) -> bool:
        if not self._running or not self._provider:
            return False
        try:
            status = await self._provider.get_device_status(device_id)
            return status.value != "offline" if hasattr(status, "value") else True
        except Exception:
            return False

    async def _watchdog_loop(self) -> None:
        """定期检测 provider 健康状态，断线时自动重连"""
        reconnect_interval = self._provider_config.get("reconnect_interval", 30.0)
        while self._running:
            await asyncio.sleep(reconnect_interval)
            if not self._running:
                break
            if not self._provider:
                await self._try_reconnect()
                continue
            # 通过简单心跳验证连接
            try:
                await asyncio.wait_for(
                    self._provider.get_device_status(list(self._devices.keys())[0] if self._devices else ""),
                    timeout=5.0,
                )
                self._provider_connected = True
                self._reconnect_count = 0
                self._reconnect_delay = _RECONNECT_BASE_DELAY
            except Exception:
                self._provider_connected = False
                await self._try_reconnect()

    async def _try_reconnect(self) -> None:
        """指数退避重连 provider"""
        if self._reconnect_count >= _RECONNECT_MAX_ATTEMPTS:
            logger.error(
                "[video] 重连次数超限(%d/%d)，停止重连。"
                "请检查 PyGBSentry 服务是否可用。",
                self._reconnect_count, _RECONNECT_MAX_ATTEMPTS,
            )
            return
        self._reconnect_count += 1
        delay = min(self._reconnect_delay * self._reconnect_count, _RECONNECT_MAX_DELAY)
        logger.warning(
            "[video] 视频连接已断开，%.1fs 后尝试第 %d 次重连...",
            delay, self._reconnect_count,
        )
        await asyncio.sleep(delay)
        if not self._running:
            return
        try:
            from edgelite.drivers.video.pygbsentry import PyGBSentryProvider
            self._provider = PyGBSentryProvider(self._provider_config)
            await self._provider.connect()
            self._provider_connected = True
            self._reconnect_count = 0
            self._reconnect_delay = _RECONNECT_BASE_DELAY
            # 重新注册所有设备
            for device_id, dev_info in list(self._devices.items()):  # FIXED-P2: 快照遍历，避免并发修改RuntimeError
                await self._provider.register_device(device_id, dev_info["config"])
            logger.info("[video] 重连成功，已重新注册 %d 个设备", len(self._devices))
        except Exception as e:
            logger.warning("[video] 重连失败: %s", e)
