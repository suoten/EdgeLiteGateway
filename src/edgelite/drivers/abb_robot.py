"""ABB机器人驱动 - 基于Robot Web Services REST API"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from edgelite.drivers.base import DriverPlugin

logger = logging.getLogger(__name__)

RWS_BASE_PATH = "/rw"
DEFAULT_PORT = 80


class AbbRobotDriver(DriverPlugin):
    """ABB机器人驱动，通过Robot Web Services (RWS) REST API通信

    配置参数:
        ip: ABB控制器IP地址
        port: RWS端口 (默认80)
        username: 用户名 (默认Default)
        password: 密码 (默认空)
    """

    plugin_name = "abb_robot"
    plugin_version = "1.0.0"
    supported_protocols = ["abb_rws"]
    config_schema = {
        "description": "ABB机器人Robot Web Services协议，通过REST API读写机器人数据",
        "fields": [
            {"name": "ip", "type": "string", "label": "IP地址", "description": "ABB控制器IP地址", "default": "192.168.1.100", "required": True},
            {"name": "port", "type": "integer", "label": "端口", "description": "RWS端口，默认80", "default": 80},
        ],
    }

    def __init__(self):
        self._running = False
        self._config: dict = {}
        self._client: Any = None
        self._lock = asyncio.Lock()
        self._base_url = ""

    async def start(self, config: dict) -> None:
        try:
            import httpx
        except ImportError:
            raise ImportError("httpx未安装，请执行: pip install httpx") from None

        self._config = config
        ip = config.get("ip", "")
        port = int(config.get("port", DEFAULT_PORT))
        if not ip:
            raise ValueError("ABB驱动配置缺少ip参数")

        self._base_url = f"http://{ip}:{port}{RWS_BASE_PATH}"
        username = config.get("username", "Default")
        password = config.get("password", "")

        self._client = httpx.AsyncClient(
            auth=(username, password) if username else None,
            timeout=httpx.Timeout(10.0, connect=5.0),
            follow_redirects=True,
        )
        self._running = True
        logger.info("ABB机器人驱动启动: %s:%d", ip, port)

    async def stop(self) -> None:
        self._running = False
        if self._client:
            await self._client.aclose()
            self._client = None
        logger.info("ABB机器人驱动已停止")

    async def _get_rapid_data(self, task: str, module: str, symbol: str) -> Any:
        """获取RAPID数据变量值
        API: GET /rw/rapid/symbol/data?task=T_ROB1&module=MainModule&symbol=x
        """
        params = {"task": task, "module": module, "symbol": symbol}
        resp = await self._client.get(f"{self._base_url}/rapid/symbol/data", params=params)
        resp.raise_for_status()
        data = resp.json()
        return self._extract_rapid_value(data, symbol)

    async def _get_joint_values(self) -> list[float]:
        """获取关节角度值
        API: GET /rw/motion/system/joint
        """
        resp = await self._client.get(f"{self._base_url}/motion/system/joint")
        resp.raise_for_status()
        data = resp.json()
        return self._extract_joint_values(data)

    async def _get_motion_data(self) -> dict[str, Any]:
        """获取运动数据 (TCP位置/速度等)
        API: GET /rw/motion/system/measurement
        """
        resp = await self._client.get(f"{self._base_url}/motion/system/measurement")
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _extract_rapid_value(data: dict, symbol: str) -> Any:
        """从RWS响应提取RAPID变量值"""
        try:
            payload = data.get("payload", data)
            if isinstance(payload, list):
                for item in payload:
                    if item.get("_symbol", "") == symbol or item.get("name", "") == symbol:
                        return item.get("value", item.get("data", None))
                return None
            value = payload.get("value", payload.get("data", None))
            if value is not None:
                return value
            for key in (symbol, "value", "data"):
                if key in payload:
                    return payload[key]
            return payload
        except Exception:
            return data

    @staticmethod
    def _extract_joint_values(data: dict) -> list[float]:
        """从RWS响应提取关节角度"""
        joints = []
        try:
            payload = data.get("payload", data)
            items = payload if isinstance(payload, list) else [payload]
            for item in items:
                for i in range(1, 7):
                    key = f"axis_{i}"
                    if key in item:
                        joints.append(float(item[key]))
                    elif f"raxis_{i}" in item:
                        joints.append(float(item[f"raxis_{i}"]))
                if "value" in item and isinstance(item["value"], (list, tuple)):
                    for v in item["value"]:
                        with contextlib.suppress(ValueError, TypeError):
                            joints.append(float(v))
        except Exception as e:
            logger.debug("ABB关节数据解析失败: %s", e)
        return joints

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        """读取ABB机器人测点值

        测点地址格式:
            "joint" / "joints" - 关节角度
            "motion" / "tcp" - 运动数据
            "RAPID:Task:Module:Symbol" - RAPID变量
        """
        if not self._running or not self._client:
            return {}

        result = {}
        for point in points:
            try:
                point_lower = point.lower()
                if point_lower in ("joint", "joints"):
                    result[point] = await self._get_joint_values()
                elif point_lower in ("motion", "tcp"):
                    result[point] = await self._get_motion_data()
                elif ":" in point:
                    parts = point.split(":")
                    if len(parts) >= 4 and parts[0].upper() == "RAPID":
                        result[point] = await self._get_rapid_data(parts[1], parts[2], parts[3])
                    else:
                        result[point] = None
                else:
                    result[point] = None
            except Exception as e:
                logger.warning("ABB读取失败 %s: %s", point, e)
                result[point] = None

        return result

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        """写入ABB RAPID变量

        地址格式: "RAPID:Task:Module:Symbol"
        """
        if not self._running or not self._client:
            return False

        if ":" not in point:
            logger.error("ABB写入地址格式无效: %s", point)
            return False

        parts = point.split(":")
        if len(parts) < 4 or parts[0].upper() != "RAPID":
            logger.error("ABB写入地址格式无效: %s", point)
            return False

        task, module, symbol = parts[1], parts[2], parts[3]

        try:
            payload = {
                "task": task,
                "module": module,
                "symbol": symbol,
                "value": str(value),
            }
            resp = await self._client.put(
                f"{self._base_url}/rapid/symbol/data",
                json=payload,
            )
            return resp.status_code in (200, 201, 204)
        except Exception as e:
            logger.error("ABB写入失败 %s: %s", point, e)
            return False
