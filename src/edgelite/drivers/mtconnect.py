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
    """

    plugin_name = "mtconnect"
    plugin_version = "1.0.0"
    supported_protocols = ["mtconnect"]
    config_schema = {
        "description": "MTConnect standard protocol for CNC equipment, retrieve CNC runtime data via HTTP",  # FIXED: 原问题-中文硬编码description
        "fields": [
            {"name": "host", "type": "string", "label": "IP Address", "description": "MTConnect agent address", "default": "127.0.0.1", "required": True},  # FIXED: 原问题-中文硬编码label/description
            {"name": "port", "type": "integer", "label": "Port", "description": "HTTP port, default 5000", "default": 5000},  # FIXED: 原问题-中文硬编码label/description
        ],
    }

    def __init__(self):
        self._running = False
        self._client: httpx.AsyncClient | None = None
        self._config: dict = {}
        self._lock = asyncio.Lock()
        self._sequence = 1  # 当前采样序列号

    async def start(self, config: dict) -> None:
        """启动MTConnect驱动"""
        self._config = config
        url = config.get("url", "")

        if not url:
            raise ValueError("MTConnect驱动配置缺少url参数")

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
        logger.info("MTConnect驱动已停止")

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        """读取MTConnect数据项

        测点名称为MTConnect数据项ID:
            如 "Xact", "Sspeed", "block", "execution"
        或使用XPath表达式: "//Axes//Linear//Position//DataItem[@name='X']"
        """
        if not self._running or not self._client:
            return {}

        result = {}
        async with self._lock:
            try:
                # 获取当前值 (Current)
                device = self._config.get("device", "")
                path = "/current"
                if device:
                    path += f"?device={device}"

                resp = await self._client.get(path)
                if resp.status_code == 200:
                    data_items = self._parse_mtconnect_response(resp.text)
                    for point in points:
                        result[point] = data_items.get(point)
                else:
                    logger.warning("MTConnect请求失败: %d", resp.status_code)
                    for point in points:
                        result[point] = None
            except Exception as e:
                logger.warning("MTConnect读取失败: %s", e)
                for point in points:
                    result[point] = None
        return result

    def _parse_mtconnect_response(self, xml_text: str) -> dict[str, Any]:
        """解析MTConnect XML响应"""
        try:
            root = ET.fromstring(xml_text)

            # MTConnect命名空间
            ns = {"mt": "urn:mtconnect.org:MTConnectStreams:1.2"}

            data_items = {}
            # 查找所有DataItem
            for component_stream in root.iter():
                for data_item in component_stream.findall(".//mt:DataItem", ns):
                    name = data_item.get("name", "")
                    data_item_id = data_item.get("dataItemId", "")
                    # 获取最新Sample/Event
                    for child in data_item:
                        value = child.text
                        if value:
                            with contextlib.suppress(ValueError, TypeError):
                                value = float(value)
                            data_items[name] = value
                            data_items[data_item_id] = value
                            break  # 取最新的

            return data_items
        except Exception as e:
            logger.error("MTConnect XML解析失败: %s", e)
            return {}

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        """MTConnect是只读协议，不支持写入"""
        logger.warning("MTConnect是只读协议，不支持写入操作")
        return False

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
