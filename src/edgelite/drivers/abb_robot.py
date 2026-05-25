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
        "description": "ABB robot Robot Web Services protocol, read/write robot data via REST API",  # FIXED: 原问题-中文硬编码description
        "fields": [
            {"name": "ip", "type": "string", "label": "IP Address", "description": "ABB controller IP address", "default": "192.168.1.100", "required": True},  # FIXED: 原问题-中文硬编码label/description
            {"name": "port", "type": "integer", "label": "Port", "description": "RWS port, default 80", "default": 80},  # FIXED: 原问题-中文硬编码label/description
        ],
    }

    _MAX_RECONNECT_ATTEMPTS = 100
    _RECONNECT_BASE_DELAY = 1.0
    _RECONNECT_MAX_DELAY = 60.0

    def __init__(self):
        self._running = False
        self._config: dict = {}
        self._client: Any = None
        self._lock = asyncio.Lock()
        self._base_url = ""
        self._connected: bool = True
        self._reconnect_count: int = 0
        self._reconnect_delay: float = self._RECONNECT_BASE_DELAY
        self._devices: dict[str, dict] = {}

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

        if not (1 <= port <= 65535):
            raise ValueError(f"ABB驱动port超出范围[1-65535]，当前: {port}")

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
            try:
                await self._client.aclose()
            except Exception as e:  # FIXED-P2: aclose异常时连接泄漏，添加异常处理
                logger.warning("ABB驱动关闭HTTP客户端异常: %s", e)
            self._client = None
        logger.info("ABB机器人驱动已停止")

    async def _get_rapid_data(self, task: str, module: str, symbol: str) -> Any:
        """获取RAPID数据变量值
        API: GET /rw/rapid/symbol/data?task=T_ROB1&module=MainModule&symbol=x
        """
        # FIXED: 原问题-httpx请求+状态码检查+JSON解析三重风险无保护
        try:
            params = {"task": task, "module": module, "symbol": symbol}
            resp = await self._client.get(f"{self._base_url}/rapid/symbol/data", params=params)
            resp.raise_for_status()
            data = resp.json()
            return self._extract_rapid_value(data, symbol)
        except Exception as e:
            logger.error("ABB _get_rapid_data failed: %s", e)
            return None

    async def _get_joint_values(self) -> list[float]:
        """获取关节角度值
        API: GET /rw/motion/system/joint
        """
        # FIXED: 原问题-httpx请求+状态码检查+JSON解析三重风险无保护
        try:
            resp = await self._client.get(f"{self._base_url}/motion/system/joint")
            resp.raise_for_status()
            data = resp.json()
            return self._extract_joint_values(data)
        except Exception as e:
            logger.error("ABB _get_joint_values failed: %s", e)
            return []

    async def _get_motion_data(self) -> dict[str, Any]:
        """获取运动数据 (TCP位置/速度等)
        API: GET /rw/motion/system/measurement
        """
        # FIXED: 原问题-httpx请求+状态码检查+JSON解析三重风险无保护
        try:
            resp = await self._client.get(f"{self._base_url}/motion/system/measurement")
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error("ABB _get_motion_data failed: %s", e)
            return {}

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

    async def add_device(self, device_id: str, config: dict, points: list[dict] | None = None) -> None:
        """添加ABB机器人设备，保存配置和测点映射"""
        if points is None:
            points = []
        self._devices[device_id] = {
            "config": config,
            "points": {p.get("name", p.get("address", "")): p for p in points if p.get("name") or p.get("address")},
        }
        logger.info("ABB设备已添加: %s (%d测点)", device_id, len(points))

    async def discover_devices(self, config: dict) -> list[dict]:
        """扫描IP段发现ABB机器人设备，通过HTTP请求Robot Web Services判断设备是否在线

        config参数:
            network: 网段地址 (如 "192.168.1.0/24") 或单个IP
            ip: 单个IP地址 (与network二选一)
            port: RWS端口 (默认80)
            username: 用户名 (默认Default)
            password: 密码 (默认空)
            timeout: 连接超时秒数 (默认3)
            max_concurrent: 最大并发数 (默认10)
        """
        try:
            import httpx
        except ImportError:
            logger.warning("httpx未安装，无法执行ABB设备发现")
            return []

        import ipaddress

        network = config.get("network", "")
        ip = config.get("ip", config.get("host", ""))
        port = int(config.get("port", DEFAULT_PORT))
        username = config.get("username", "Default")
        password = config.get("password", "")
        timeout = float(config.get("timeout", 3.0))
        max_concurrent = int(config.get("max_concurrent", 10))

        if network:
            try:
                net = ipaddress.ip_network(network, strict=False)
                ips = [str(addr) for addr in net.hosts()]
            except ValueError as e:
                logger.error("ABB发现: 无效的网段 %s - %s", network, e)
                return []
        elif ip:
            ips = [ip]
        else:
            logger.warning("ABB发现: 未指定network或ip参数")
            return []

        discovered = []
        sem = asyncio.Semaphore(max_concurrent)

        async def _probe(ip_addr: str) -> dict | None:
            async with sem:
                try:
                    async with httpx.AsyncClient(
                        auth=(username, password) if username else None,
                        timeout=httpx.Timeout(timeout, connect=timeout),
                        follow_redirects=True,
                    ) as client:
                        base_url = f"http://{ip_addr}:{port}{RWS_BASE_PATH}"
                        resp = await client.get(f"{base_url}/system")
                        if resp.status_code == 200:
                            robot_name = ""
                            try:
                                data = resp.json()
                                payload = data.get("payload", data)
                                if isinstance(payload, list):
                                    for item in payload:
                                        name = item.get("name", item.get("_title", ""))
                                        if name:
                                            robot_name = name
                                            break
                                elif isinstance(payload, dict):
                                    robot_name = payload.get("name", payload.get("_title", ""))
                            except Exception:
                                pass
                            return {
                                "device_id": f"abb_{ip_addr.replace('.', '_')}",
                                "name": f"ABB Robot ({ip_addr})" + (f" - {robot_name}" if robot_name else ""),
                                "protocol": "abb_rws",
                                "config": {
                                    "ip": ip_addr,
                                    "port": port,
                                },
                                "points": [],
                                "details": {
                                    "robot_name": robot_name,
                                },
                            }
                except Exception:
                    return None
                return None

        tasks = [_probe(addr) for addr in ips]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for r in results:
            if isinstance(r, dict):
                discovered.append(r)

        logger.info("ABB设备发现完成: 扫描%d个IP, 发现%d台设备", len(ips), len(discovered))
        return discovered

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        """读取ABB机器人测点值

        测点地址格式:
            "joint" / "joints" - 关节角度
            "motion" / "tcp" - 运动数据
            "RAPID:Task:Module:Symbol" - RAPID变量
        """
        if not self._running or not self._client or not self._connected:
            await self._ensure_client(device_id)
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
                self._connected = False
                result[point] = None

        return result

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        """写入ABB RAPID变量

        地址格式: "RAPID:Task:Module:Symbol"
        """
        if not self._running or not self._client or not self._connected:
            await self._ensure_client(device_id)
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
            self._connected = False
            return False

    async def _ensure_client(self, device_id: str) -> None:
        if not self._config:
            return
        self._reconnect_count += 1
        if self._reconnect_count > self._MAX_RECONNECT_ATTEMPTS:
            logger.error("ABB重连放弃: %s (已重试%d次)", device_id, self._reconnect_count)
            self._running = False
            return
        delay = min(self._reconnect_delay, self._RECONNECT_MAX_DELAY)
        logger.warning("ABB连接断开，%.1fs后重建客户端 (第%d次): %s", delay, self._reconnect_count, device_id)
        await asyncio.sleep(delay)
        self._reconnect_delay *= 2
        ip = self._config.get("ip", "")
        port = int(self._config.get("port", DEFAULT_PORT))
        if not ip:
            return
        self._base_url = f"http://{ip}:{port}{RWS_BASE_PATH}"
        username = self._config.get("username", "Default")
        password = self._config.get("password", "")
        if self._client:
            try:
                await self._client.aclose()
            except Exception:
                pass
        try:
            import httpx
            self._client = httpx.AsyncClient(
                auth=(username, password) if username else None,
                timeout=httpx.Timeout(10.0, connect=5.0),
                follow_redirects=True,
            )
            self._running = True
            self._connected = True
            self._reconnect_count = 0
            self._reconnect_delay = self._RECONNECT_BASE_DELAY
            logger.info("ABB客户端重建成功: %s:%d", ip, port)
        except Exception as e:
            logger.error("ABB客户端重建失败: %s - %s", ip, e)

    def remove_device(self, device_id: str) -> None:
        """Remove a device at runtime"""
        self._health_stats.pop(device_id, None)
        self._offline_since.pop(device_id, None)
        logger.info("ABB robot device removed: %s", device_id)
