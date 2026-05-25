"""Allen-Bradley PLC驱动 - 基于pylogix库，支持ControlLogix/CompactLogix/MicroLogix"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from edgelite.constants import _ALLEN_BRADLEY_DEFAULT_PORT
from edgelite.drivers.base import DriverPlugin

logger = logging.getLogger(__name__)


class AllenBradleyDriver(DriverPlugin):
    """Allen-Bradley PLC驱动 (罗克韦尔自动化)

    配置参数:
        ip: PLC IP地址
        port: 端口号 (默认44818 for CIP, 2222 for PCCC)
        slot: ControlLogix槽号 (默认0, CompactLogix默认0)
        micrologix: 是否为MicroLogix/PCCC协议 (默认False)
    """

    plugin_name = "allen_bradley"
    plugin_version = "1.0.0"
    supported_protocols = ["ab", "ab_cip", "ab_pccc"]
    config_schema = {
        "description": "Allen-Bradley PLC protocol (pylogix), supports ControlLogix/CompactLogix",
        "fields": [
            {"name": "ip", "type": "string", "label": "IP Address", "description": "AB PLC IP address", "default": "192.168.1.1", "required": True},
            {"name": "port", "type": "integer", "label": "Port", "description": "CIP port (default 44818), PCCC/MicroLogix uses 2222", "default": 44818},
            {"name": "slot", "type": "integer", "label": "Slot", "description": "CPU slot position, ControlLogix default 0, CompactLogix default 0", "default": 0},
        ],
    }

    _MAX_RECONNECT_ATTEMPTS = 100
    _RECONNECT_BASE_DELAY = 1.0
    _RECONNECT_MAX_DELAY = 60.0

    def __init__(self):
        self._running = False
        self._client = None
        self._config: dict = {}
        self._lock = asyncio.Lock()
        self._reconnect_count: int = 0
        self._reconnect_delay: float = self._RECONNECT_BASE_DELAY
        self._devices: dict[str, dict] = {}

    async def start(self, config: dict) -> None:
        """启动AB PLC驱动连接"""
        try:
            from pylogix import PLC
        except ImportError:
            raise ImportError("pylogix未安装，请执行: pip install pylogix") from None

        self._config = config
        ip = config.get("ip") or config.get("host", "")
        port = int(config.get("port", 44818))
        slot = int(config.get("slot", 0))
        micrologix = config.get("micrologix", False)

        if not ip:
            raise ValueError("AB driver config missing 'ip' parameter")

        if not (1 <= port <= 65535):
            raise ValueError(f"AB驱动port超出范围[1-65535]，当前: {port}")
        if not (0 <= slot <= 31):
            raise ValueError(f"AB驱动slot超出范围[0-31]，当前: {slot}")

        try:
            self._client = PLC(ip=ip, port=port, slot=slot)
            self._running = True
            logger.info("AB驱动连接成功: %s (slot=%d, micrologix=%s)", ip, slot, micrologix)
        except Exception as e:
            logger.error("AB驱动连接失败: %s - %s", ip, e)
            raise

    async def stop(self) -> None:
        """停止AB驱动"""
        self._running = False
        if self._client:
            try:
                await asyncio.to_thread(self._client.Close)
            except Exception as e:
                logger.warning("AB驱动断开异常: %s", e)
            self._client = None
        logger.info("AB驱动已停止")

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        """读取AB PLC测点值

        测点地址格式为AB标签名:
            - ControlLogix/CompactLogix: "Program:Main.TagName" 或 "TagName"
            - MicroLogix: "N7:0" (整数), "F8:0" (浮点), "B3:0" (位)
        """
        if not self._running or not self._client:
            await self._try_reconnect(device_id)
            return {}

        result = {}
        async with self._lock:
            try:
                # pylogix支持批量读取，返回值可能是单个Response或Response列表
                response = await asyncio.to_thread(self._client.Read, points)
                if isinstance(response, list):
                    for i, point in enumerate(points):
                        if i < len(response):
                            resp_item = response[i]
                            # FIXED: P2-1 pylogix Response对象可能有属性访问问题，添加安全检查
                            if hasattr(resp_item, 'Value') and hasattr(resp_item, 'Status'):
                                result[point] = resp_item.Value if resp_item.Status == 0 else None
                            elif hasattr(resp_item, 'value'):
                                result[point] = resp_item.value
                            else:
                                result[point] = resp_item
                        else:
                            result[point] = None
                else:
                    # 单个读取结果
                    resp_item = response
                    if hasattr(resp_item, 'Value') and hasattr(resp_item, 'Status'):
                        result[points[0]] = resp_item.Value if resp_item.Status == 0 else None
                    elif hasattr(resp_item, 'value'):
                        result[points[0]] = resp_item.value
                    else:
                        result[points[0]] = resp_item
            except Exception as e:
                logger.warning("AB批量读取失败，尝试逐个读取: %s", e)
                for point_addr in points:
                    try:
                        resp = await asyncio.to_thread(self._client.Read, point_addr)
                        if hasattr(resp, 'Value') and hasattr(resp, 'Status'):
                            result[point_addr] = resp.Value if resp.Status == 0 else None
                        elif hasattr(resp, 'value'):
                            result[point_addr] = resp.value
                        else:
                            result[point_addr] = resp
                    except Exception as e2:
                        logger.warning("AB读取失败 %s: %s", point_addr, e2)
                        result[point_addr] = None
        return result

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        """写入AB PLC标签值"""
        if not self._running or not self._client:
            await self._try_reconnect(device_id)
            return False

        try:
            async with self._lock:
                response = await asyncio.to_thread(self._client.Write, point, value)
                return response.Status == 0
        except Exception as e:
            logger.error("AB写入失败 %s: %s", point, e)
            return False

    async def _try_reconnect(self, device_id: str) -> None:
        if not self._config:
            return
        self._reconnect_count += 1
        if self._reconnect_count > self._MAX_RECONNECT_ATTEMPTS:
            logger.error("AB重连放弃: %s (已重试%d次)", device_id, self._reconnect_count)
            self._running = False
            return
        delay = min(self._reconnect_delay, self._RECONNECT_MAX_DELAY)
        logger.warning("AB连接断开，%.1fs后重连 (第%d次): %s", delay, self._reconnect_count, device_id)
        await asyncio.sleep(delay)
        self._reconnect_delay *= 2
        ip = self._config.get("ip") or self._config.get("host", "")
        port = int(self._config.get("port", 44818))
        slot = int(self._config.get("slot", 0))
        if not ip:
            return
        if self._client:
            try:
                await asyncio.to_thread(self._client.Close)
            except Exception:
                pass
        try:
            from pylogix import PLC
            self._client = PLC(ip=ip, port=port, slot=slot)
            self._running = True
            self._reconnect_count = 0
            self._reconnect_delay = self._RECONNECT_BASE_DELAY
            logger.info("AB重连成功: %s:%d (slot=%d)", ip, port, slot)
        except Exception as e:
            logger.error("AB重连失败: %s - %s", ip, e)

    async def add_device(self, device_id: str, config: dict, points: list[dict] | None = None) -> None:
        """添加AB PLC设备，保存配置和标签映射"""
        if points is None:
            points = []
        self._devices[device_id] = {
            "config": config,
            "points": {p.get("name", p.get("address", "")): p for p in points if p.get("name") or p.get("address")},
        }
        logger.info("AB设备已添加: %s (%d测点)", device_id, len(points))

    async def discover_devices(self, config: dict) -> list[dict]:
        """AB PLC发现 - 获取控制器信息"""
        if not self._client:
            return []

        try:
            # 读取控制器信息
            ip = self._config.get("ip", "")
            slot = self._config.get("slot", 0)

            # 尝试读取项目名称
            response = await asyncio.to_thread(self._client.Read, "ProgramName")
            project_name = response.Value if response.Status == 0 else "Unknown"

            return [
                {
                    "device_id": f"ab_{ip.replace('.', '_')}",
                    "name": f"AB PLC ({project_name})",
                    "ip": ip,
                    "protocol": "ab",
                    "slot": slot,
                }
            ]
        except Exception as e:
            logger.error("AB设备发现失败: %s", e)
            return []

    async def discover_tags(self, program: str = "") -> list[dict]:
        """发现PLC标签（AB特有功能）

        Args:
            program: 程序名，空字符串表示控制器标签
        """
        if not self._client:
            return []

        try:
            response = await asyncio.to_thread(self._client.GetTagList, program)
            tags = []
            for tag in response.Value or []:
                tags.append(
                    {
                        "name": tag.TagName,
                        "type": tag.DataType,
                        "dimensions": tag.ArrayDimensions,
                    }
                )
            return tags
        except Exception as e:
            logger.error("AB标签发现失败: %s", e)
            return []

    def remove_device(self, device_id: str) -> None:
        """Remove a device at runtime"""
        self._health_stats.pop(device_id, None)
        self._offline_since.pop(device_id, None)
        logger.info("AB device removed: %s", device_id)
