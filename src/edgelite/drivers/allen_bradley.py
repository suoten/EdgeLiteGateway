"""Allen-Bradley PLC驱动 - 基于pylogix库，支持ControlLogix/CompactLogix/MicroLogix"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

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

    def __init__(self):
        self._running = False
        self._client = None
        self._config: dict = {}
        self._lock = asyncio.Lock()

    async def start(self, config: dict) -> None:
        """启动AB PLC驱动连接"""
        try:
            from pylogix import PLC
        except ImportError:
            raise ImportError("pylogix未安装，请执行: pip install pylogix") from None

        self._config = config
        ip = config.get("ip", "")
        slot = int(config.get("slot", 0))
        micrologix = config.get("micrologix", False)

        if not ip:
            raise ValueError("AB驱动配置缺少ip参数")

        try:
            if micrologix:
                self._client = PLC(ip=ip, port=2222)
            else:
                self._client = PLC(ip=ip, slot=slot)
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
            return {}

        result = {}
        async with self._lock:
            try:
                # pylogix支持批量读取
                response = await asyncio.to_thread(self._client.Read, points)
                if isinstance(response, list):
                    for i, point in enumerate(points):
                        if i < len(response):
                            result[point] = response[i].Value
                        else:
                            result[point] = None
                else:
                    # 单个读取
                    result[points[0]] = response.Value
            except Exception as e:
                logger.warning("AB批量读取失败，尝试逐个读取: %s", e)
                for point_addr in points:
                    try:
                        resp = await asyncio.to_thread(self._client.Read, point_addr)
                        result[point_addr] = resp.Value
                    except Exception as e2:
                        logger.warning("AB读取失败 %s: %s", point_addr, e2)
                        result[point_addr] = None
        return result

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        """写入AB PLC标签值"""
        if not self._running or not self._client:
            return False

        try:
            async with self._lock:
                response = await asyncio.to_thread(self._client.Write, point, value)
                return response.Status == 0
        except Exception as e:
            logger.error("AB写入失败 %s: %s", point, e)
            return False

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
