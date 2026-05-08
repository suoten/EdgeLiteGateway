"""PyGBSentry视频平台适配器"""

from __future__ import annotations

import logging
from collections.abc import Callable

import httpx

from edgelite.config import get_config
from edgelite.drivers.video.provider import DeviceStatus, VideoProvider

logger = logging.getLogger(__name__)


class PyGBSentryProvider(VideoProvider):
    """PyGBSentry视频平台适配器"""

    def __init__(self):
        config = get_config()
        self._endpoint = config.video.pygbsentry.endpoint
        self._api_key = config.video.pygbsentry.api_key
        self._timeout = config.video.pygbsentry.timeout
        self._client: httpx.AsyncClient | None = None
        self._alarm_callback: Callable | None = None

    async def connect(self) -> None:
        """建立HTTP连接"""
        if not self._endpoint:
            logger.warning("PyGBSentry endpoint未配置")
            return
        self._client = httpx.AsyncClient(
            base_url=self._endpoint,
            headers={"Authorization": f"Bearer {self._api_key}"} if self._api_key else {},
            timeout=self._timeout,
        )
        logger.info("PyGBSentry适配器连接: %s", self._endpoint)

    async def close(self) -> None:
        """关闭连接"""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def register_device(self, device_id: str, config: dict) -> bool:
        """注册视频设备（验证设备在PyGBSentry中存在）"""
        if not self._client:
            return False
        try:
            resp = await self._client.get(f"/api/v1/devices/{device_id}")
            if resp.status_code == 200:
                logger.info("视频设备注册成功: %s", device_id)
                return True
            else:
                logger.warning(
                    "视频设备在PyGBSentry中不存在: %s (status=%d)", device_id, resp.status_code
                )
                return False
        except Exception as e:
            logger.error("视频设备注册失败: %s - %s", device_id, e)
            return False

    async def get_stream_url(self, device_id: str, channel_id: str) -> str:
        """获取视频流地址"""
        if not self._client:
            return ""
        try:
            resp = await self._client.get(
                f"/api/v1/devices/{device_id}/channels/{channel_id}/stream"
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("data", {}).get("url", "")
            return ""
        except Exception as e:
            logger.error("获取视频流地址失败: %s - %s", device_id, e)
            return ""

    async def ptz_control(self, device_id: str, channel_id: str, action: str, **kwargs) -> bool:
        """云台控制"""
        if not self._client:
            return False
        try:
            resp = await self._client.post(
                f"/api/v1/devices/{device_id}/channels/{channel_id}/ptz",
                json={"action": action, **kwargs},
            )
            return resp.status_code == 200
        except Exception as e:
            logger.error("云台控制失败: %s - %s", device_id, e)
            return False

    async def get_device_status(self, device_id: str) -> DeviceStatus:
        """获取视频设备状态"""
        if not self._client:
            return DeviceStatus.UNKNOWN
        try:
            resp = await self._client.get(f"/api/v1/devices/{device_id}/status")
            if resp.status_code == 200:
                data = resp.json()
                status = data.get("data", {}).get("status", "offline")
                return DeviceStatus.ONLINE if status == "online" else DeviceStatus.OFFLINE
            return DeviceStatus.UNKNOWN
        except Exception:
            return DeviceStatus.UNKNOWN

    async def on_alarm(self, callback: Callable) -> None:
        """注册告警回调"""
        self._alarm_callback = callback

    async def handle_webhook(self, event_data: dict) -> None:
        """处理PyGBSentry Webhook回调"""
        logger.info("收到PyGBSentry Webhook: %s", event_data.get("type", "unknown"))
        if self._alarm_callback:
            await self._alarm_callback(event_data)
