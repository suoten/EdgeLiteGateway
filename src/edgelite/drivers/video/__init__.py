"""视频接入抽象层 + VideoDriver"""

from __future__ import annotations

from typing import Any

from edgelite.drivers.base import DriverPlugin


class VideoDriver(DriverPlugin):
    plugin_name = "video"
    plugin_version = "1.0.0"
    supported_protocols = ["gb28181", "video"]
    config_schema = {
        "description": "GB28181 video surveillance protocol, access video streams and PTZ control via PyGBSentry",  # FIXED: 原问题-中文硬编码description
        "fields": [
            {"name": "pygbsentry_url", "type": "string", "label": "PyGBSentry Address", "description": "PyGBSentry API address", "default": "http://127.0.0.1:8080", "required": True},  # FIXED: 原问题-中文硬编码label/description
            {"name": "username", "type": "string", "label": "Username", "description": "PyGBSentry login username", "default": "admin"},  # FIXED: 原问题-中文硬编码label/description
            {"name": "password", "type": "string", "label": "Password", "description": "PyGBSentry login password", "secret": True},  # FIXED: 原问题-中文硬编码label/description
        ],
    }

    def __init__(self):
        self._provider = None
        self._devices: dict[str, dict] = {}
        self._data_callback = None

    async def start(self, config: dict) -> None:
        from edgelite.drivers.video.pygbsentry import PyGBSentryProvider

        self._provider = PyGBSentryProvider(config)
        await self._provider.connect()

    async def stop(self) -> None:
        if self._provider:
            await self._provider.close()
            self._provider = None

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        result = {}
        if not self._provider:
            return result
        try:
            status = await self._provider.get_device_status(device_id)
            if "status" in points:
                result["status"] = status.value if hasattr(status, "value") else str(status)
            if "stream_url" in points:
                try:
                    url = await self._provider.get_stream_url(device_id, "1")
                    result["stream_url"] = url or ""
                except Exception:
                    result["stream_url"] = ""
        except Exception:
            for p in points:
                result[p] = None
        return result

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        if not self._provider:
            return False
        try:
            if point.startswith("ptz_"):
                action = point.replace("ptz_", "")
                return await self._provider.ptz_control(
                    device_id,
                    "1",
                    action,
                    **({"speed": value} if isinstance(value, (int, float)) else {}),
                )
            return False
        except Exception:
            return False

    async def discover_devices(self, config: dict) -> list[dict]:
        return []

    def on_data(self, callback) -> None:
        self._data_callback = callback
