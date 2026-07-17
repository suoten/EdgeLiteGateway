"""MTConnect协议驱动 - 数控机床标准通信协议

MTConnect是制造业开放标准协议，提供CNC机床数据的统一访问方式。
通过HTTP协议获取设备当前状态、采样数据和事件。
支持所有兼容MTConnect标准的CNC控制器和制造设备。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

try:
    import httpx
except ImportError:
    httpx = None

try:
    from lxml import etree as ET  # C扩展，比ElementTree快5-10倍
except ImportError:
    import xml.etree.ElementTree as ET  # 回退到纯Python实现

import contextlib

from edgelite.drivers.base import DriverPlugin

logger = logging.getLogger(__name__)


class MTConnectDriver(DriverPlugin):
    """MTConnect协议驱动

    配置参数:
        url: MTConnect Agent的HTTP地址 (如"http://192.168.1.100:5000")
        device: 设备名称 (可选，多设备Agent时指定)
        poll_interval: 轮询间隔秒 (默认1)
        use_agent: 是否使用Agent推送模式 (默认False，使用轮询)
        enable_availability: 是否启用可用性监测 (默认True)
    """

    plugin_name = "mtconnect"
    plugin_version = "1.1.0"
    supported_protocols = ("mtconnect",)  # FIXED(P2): 原问题-可变默认值list; 修复-改为tuple
    config_schema = {
        "description": "MTConnect standard protocol for CNC equipment, retrieve CNC runtime data via HTTP",
        "fields": [
            {
                "name": "url",
                "type": "string",
                "label": "Agent URL",
                "description": "MTConnect agent HTTP URL, e.g. http://192.168.1.100:5000",
                "default": "http://127.0.0.1:5000",
                "required": True,
            },
            {
                "name": "device",
                "type": "string",
                "label": "Device Name",
                "description": "Device name to read (optional, for multi-device agents)",
                "default": "",
            },
            {
                "name": "poll_interval",
                "type": "integer",
                "label": "Poll Interval (s)",
                "description": "Polling interval in seconds",
                "default": 1,
            },
            {
                "name": "enable_availability",
                "type": "boolean",
                "label": "Enable Availability",
                "description": "Monitor device availability status",
                "default": True,
            },
        ],
    }

    _MAX_RECONNECT_ATTEMPTS = 100
    _RECONNECT_BASE_DELAY = 1.0
    _RECONNECT_MAX_DELAY = 60.0

    def __init__(self):
        super().__init__()  # FIXED-P0: 必须调用基类初始化
        self._running = False
        self._client: httpx.AsyncClient | None = None
        self._config: dict = {}
        self._lock = asyncio.Lock()
        self._sequence = 1
        self._connected: bool = True
        self._reconnect_count: int = 0
        self._reconnect_delay: float = self._RECONNECT_BASE_DELAY
        # 新增: 错误追踪
        self._error_count: int = 0
        self._last_success_time: float = 0
        self._consecutive_errors: int = 0
        # 新增: 数据回调
        self._data_callback: Any = None
        self._latest_values: dict[str, Any] = {}
        self._devices: dict[str, dict] = {}

    async def start(self, config: dict) -> None:
        """启动MTConnect驱动"""
        if httpx is None:  # FIXED-P2: httpx未安装时直接调用httpx.AsyncClient抛TypeError，改为明确ImportError
            raise ImportError("MTConnect驱动需要httpx库，请安装: pip install httpx")
        self._config = config
        url = config.get("url", "")

        if not url:
            raise ValueError("MTConnect驱动配置缺少url参数")

        if not (url.startswith("http://") or url.startswith("https://")):
            raise ValueError(f"MTConnect驱动url必须以http://或https://开头，当前: {url}")

        self._client = httpx.AsyncClient(
            base_url=url.rstrip("/"),
            timeout=httpx.Timeout(10.0),
        )
        self._running = True
        logger.info("MTConnect驱动启动: %s", url)

    async def stop(self) -> None:
        """停止MTConnect驱动"""
        self._running = False
        if self._client:
            await self._client.aclose()
            self._client = None
        await super().stop()  # FIXED-P0: 清理基类资源
        logger.info("MTConnect驱动已停止")

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        """读取MTConnect数据项

        测点名称为MTConnect数据项ID:
            如 "Xact", "Sspeed", "block", "execution"
        或使用XPath表达式: "//Axes//Linear//Position//DataItem[@name='X']"
        """
        if not self._running or not self._client or not self._connected:
            await self._ensure_client(device_id)
            return {}

        result = {}
        async with self._lock:
            try:
                device = self._config.get("device", "")
                path = f"/sample?from={self._sequence}"
                if device:
                    path += f"&device={device}"

                resp = await self._client.get(path)
                if resp.status_code == 200:
                    data_items, next_seq = self._parse_mtconnect_response(resp.text, extract_sequence=True)
                    if next_seq:
                        self._sequence = next_seq
                    if data_items:
                        for point in points:
                            result[point] = data_items.get(point)
                    else:
                        result = await self._read_current(points, device)
                else:
                    result = await self._read_current(points, device)
            except Exception as e:
                logger.warning("MTConnect Sample读取失败，回退Current: %s", e)
                result = await self._read_current(points, self._config.get("device", ""))
        return result

    async def _read_current(self, points: list[str], device: str) -> dict[str, Any]:
        result = {}
        try:
            path = "/current"
            if device:
                path += f"?device={device}"

            resp = await self._client.get(path)
            if resp.status_code == 200:
                data_items = self._parse_mtconnect_response(resp.text)
                for point in points:
                    result[point] = data_items.get(point)
                # 成功读取，更新状态
                self._consecutive_errors = 0
                self._connected = True
                self._last_success_time = asyncio.get_event_loop().time()
                # 缓存最新值
                for name, value in data_items.items():
                    self._latest_values[name] = value
                # 触发回调
                if self._data_callback and data_items:
                    await self._data_callback(device, data_items)
            else:
                self._consecutive_errors += 1
                logger.warning("MTConnect请求失败: %d", resp.status_code)
                for point in points:
                    result[point] = self._latest_values.get(point)
        except Exception as e:
            self._consecutive_errors += 1
            self._error_count += 1
            logger.warning("MTConnect Current读取失败: %s", e)
            self._connected = False
            for point in points:
                result[point] = self._latest_values.get(point)
        return result

    def _parse_mtconnect_response(
        self, xml_text: str, extract_sequence: bool = False
    ) -> dict[str, Any] | tuple[dict[str, Any], int | None]:
        """解析MTConnect XML响应"""
        try:
            root = ET.fromstring(xml_text)

            next_sequence = None
            if extract_sequence:
                header = root.find(".//{urn:mtconnect.org:MTConnectStreams:1.2}Header")
                if header is None:
                    for child in root:
                        if child.tag.endswith("Header") or "Header" in child.tag:
                            header = child
                            break
                if header is not None:
                    with contextlib.suppress(ValueError, TypeError):
                        next_sequence = int(header.get("nextSequence", ""))

            ns = {"mt": "urn:mtconnect.org:MTConnectStreams:1.2"}

            data_items = {}
            for component_stream in root.iter():
                for data_item in component_stream.findall(".//mt:DataItem", ns):
                    name = data_item.get("name", "")
                    data_item_id = data_item.get("dataItemId", "")
                    for child in data_item:
                        value = child.text
                        if value:
                            with contextlib.suppress(ValueError, TypeError):
                                value = float(value)
                            data_items[name] = value
                            data_items[data_item_id] = value
                            break

            if extract_sequence:
                return data_items, next_sequence
            return data_items
        except Exception as e:
            logger.error("MTConnect XML解析失败: %s", e)
            if extract_sequence:
                return {}, None
            return {}

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        """MTConnect是只读协议，不支持写入"""
        logger.warning("MTConnect是只读协议，不支持写入操作")
        return False

    async def _ensure_client(self, device_id: str) -> None:
        if not self._config:
            return
        self._reconnect_count += 1
        if self._reconnect_count > self._MAX_RECONNECT_ATTEMPTS:
            logger.error("MTConnect重连放弃: %s (已重试%d次)", device_id, self._reconnect_count)
            self._running = False
            return
        delay = min(self._reconnect_delay, self._RECONNECT_MAX_DELAY)
        logger.warning("MTConnect连接断开，%.1fs后重建客户端 (第%d次): %s", delay, self._reconnect_count, device_id)
        await asyncio.sleep(delay)
        self._reconnect_delay *= 2
        url = self._config.get("url", "")
        if not url:
            return
        if self._client:
            try:
                await self._client.aclose()
            except Exception as e:
                logger.debug("[mtconnect] client close failed: %s", e)
        try:
            if httpx is None:
                return
            self._client = httpx.AsyncClient(
                base_url=url.rstrip("/"),
                timeout=httpx.Timeout(10.0),
            )
            self._running = True
            self._connected = True
            self._reconnect_count = 0
            self._reconnect_delay = self._RECONNECT_BASE_DELAY
            logger.info("MTConnect客户端重建成功: %s", url)
        except Exception as e:
            logger.error("MTConnect客户端重建失败: %s - %s", url, e)

    async def add_device(self, device_id: str, config: dict, points: list[dict] | None = None) -> None:
        """添加MTConnect设备，保存配置和测点映射"""
        if points is None:
            points = []
        self._devices[device_id] = {
            "config": config,
            "points": {p.get("name", p.get("address", "")): p for p in points if p.get("name") or p.get("address")},
        }
        logger.info("MTConnect设备已添加: %s (%d测点)", device_id, len(points))

    async def discover_devices(self, config: dict) -> list[dict]:
        """发现MTConnect Agent上的设备"""
        if not self._client:
            return []

        try:
            resp = await self._client.get("/probe")
            if resp.status_code != 200:
                return []

            try:
                root = ET.fromstring(resp.text)
            except Exception as e:
                logger.warning("MTConnect XML解析失败: %s", e)
                return []

            ns = {"mt": "urn:mtconnect.org:MTConnectDevices:1.2"}

            devices = []
            for device in root.findall(".//mt:Device", ns):
                devices.append(
                    {
                        "device_id": device.get("id", ""),
                        "name": device.get("name", ""),
                        "uuid": device.get("uuid", ""),
                        "protocol": "mtconnect",
                    }
                )
            return devices
        except Exception as e:
            logger.error("MTConnect设备发现失败: %s", e)
            return []

    def on_data(self, callback) -> None:
        """注册数据回调，支持推送模式"""
        self._data_callback = callback

    def is_device_connected(self, device_id: str) -> bool:
        """检查MTConnect Agent连接状态"""
        return self._connected and self._client is not None

    def get_connection_stats(self) -> dict:
        """获取连接统计信息，用于监控"""
        return {
            "connected": self._connected,
            "error_count": self._error_count,
            "consecutive_errors": self._consecutive_errors,
            "last_success_time": self._last_success_time,
            "reconnect_count": self._reconnect_count,
        }

    async def browse_paths(self, path: str = "//") -> list[dict]:
        """浏览MTConnect数据项路径

        Args:
            path: XPath路径，默认为全部

        Returns:
            数据项列表 [{"name": "...", "id": "...", "type": "..."}]
        """
        if not self._client:
            return []

        try:
            device = self._config.get("device", "")
            url_path = "/current"
            if device:
                url_path += f"?device={device}"

            resp = await self._client.get(url_path)
            if resp.status_code != 200:
                return []

            root = ET.fromstring(resp.text)
            items = []

            for elem in root.iter():
                tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
                if tag == "DataItem":
                    items.append(
                        {
                            "name": elem.get("name", ""),
                            "id": elem.get("dataItemId", ""),
                            "type": elem.get("type", ""),
                            "category": elem.get("category", ""),
                            "subType": elem.get("subType", ""),
                        }
                    )

            return items
        except Exception as e:
            logger.error("MTConnect路径浏览失败: %s", e)
            return []

    def remove_device(self, device_id: str) -> None:
        """Remove a device at runtime"""
        self._health_stats.pop(device_id, None)
        self._offline_since.pop(device_id, None)
        self._latest_values.clear()
        logger.info("MTConnect device removed: %s", device_id)
