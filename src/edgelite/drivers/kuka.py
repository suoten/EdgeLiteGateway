"""KUKA机器人驱动 - 基于Ethernet KRL XML协议"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import xml.etree.ElementTree as ET
from typing import Any

from edgelite.drivers.base import DriverPlugin

logger = logging.getLogger(__name__)

DEFAULT_EKRL_PORT = 54600
RECV_BUFFER_SIZE = 4096
BACKOFF_BASE = 1.0
BACKOFF_MAX = 30.0


class KukaDriver(DriverPlugin):
    """KUKA机器人驱动，通过Ethernet KRL XML协议通信

    配置参数:
        ip: KUKA控制器IP地址
        port: EKRL端口 (默认54600)
        username: 用户名 (可选)
        password: 密码 (可选)
        reconnect: 是否自动重连 (默认True)
    """

    plugin_name = "kuka"
    plugin_version = "1.0.0"
    supported_protocols = ["kuka_ekrl"]
    config_schema = {
        "description": "KUKA robot Ethernet KRL XML protocol for reading and writing robot variables",  # FIXED: 原问题-中文硬编码description
        "fields": [
            {
                "name": "ip",
                "type": "string",
                "label": "IP Address",
                "description": "KUKA controller IP address",
                "default": "192.168.1.100",
                "required": True,
            },  # FIXED: 原问题-中文硬编码label/description
            {
                "name": "port",
                "type": "integer",
                "label": "Port",
                "description": "EKRL port, default 54600",
                "default": 54600,
            },  # FIXED: 原问题-中文硬编码label/description
        ],
    }

    def __init__(self):
        super().__init__()  # FIXED-P0: 必须调用基类初始化
        self._running = False
        self._config: dict = {}
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._connect_task: asyncio.Task | None = None
        self._recv_task: asyncio.Task | None = None
        self._connected = False
        self._backoff_delay = BACKOFF_BASE
        self._devices: dict[str, dict] = {}

    async def start(self, config: dict) -> None:
        self._config = config
        self._running = True
        ip = config.get("ip", "")
        if not ip:
            raise ValueError("KUKA驱动配置缺少ip参数")

        if config.get("reconnect", True):
            self._connect_task = asyncio.create_task(self._connect_loop(), name="kuka-connect")
        else:
            await self._connect_once()
        logger.info("KUKA驱动启动: %s", ip)

    async def stop(self) -> None:
        self._running = False
        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._recv_task
        self._recv_task = None
        if self._connect_task and not self._connect_task.done():
            self._connect_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._connect_task
        self._connect_task = None
        await self._disconnect()
        await super().stop()  # FIXED-P0: 清理基类资源
        logger.info("KUKA驱动已停止")

    async def _connect_once(self) -> bool:
        """单次连接尝试"""
        ip = self._config.get("ip", "")
        port = int(self._config.get("port", DEFAULT_EKRL_PORT))
        try:
            self._reader, self._writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=5.0)
            self._connected = True
            self._backoff_delay = BACKOFF_BASE
            logger.info("KUKA连接成功: %s:%d", ip, port)
            return True
        except Exception as e:
            logger.warning("KUKA连接失败: %s:%d - %s", ip, port, e)
            self._connected = False
            return False

    async def _connect_loop(self) -> None:
        """指数退避重连循环"""
        while self._running:
            if not self._connected:
                if await self._connect_once():
                    # FIXED: P0-7 连接成功后启动数据接收任务（之前遗漏）
                    if self._running and self._connected:
                        self._recv_task = asyncio.create_task(self._recv_loop(), name="kuka-recv")
                    continue
                logger.info("KUKA %.1fs后重连...", self._backoff_delay)
                await asyncio.sleep(self._backoff_delay)
                self._backoff_delay = min(self._backoff_delay * 2, BACKOFF_MAX)
            else:
                try:
                    if self._writer:
                        keepalive_xml = self._build_ekrl_read_request(["$POS_ACT"])
                        self._writer.write(keepalive_xml.encode("utf-8"))
                        await self._writer.drain()
                    if self._reader:
                        with contextlib.suppress(asyncio.TimeoutError):
                            await asyncio.wait_for(self._reader.read(RECV_BUFFER_SIZE), timeout=0.1)
                    await asyncio.sleep(10)
                except Exception:
                    self._connected = False
                    self._backoff_delay = BACKOFF_BASE

    async def _disconnect(self) -> None:
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception as e:
                logger.debug("KUKA连接关闭失败: %s", e)
        self._reader = None
        self._writer = None
        self._connected = False

    def _build_ekrl_read_request(self, variables: list[str]) -> str:
        """构造Ethernet KRL XML读请求
        格式: <Robot><Read><Variable Name="xxx"/>...</Read></Robot>
        """
        root = ET.Element("Robot")
        read_elem = ET.SubElement(root, "Read")
        for var_name in variables:
            ET.SubElement(read_elem, "Variable", Name=var_name)
        return ET.tostring(root, encoding="unicode") + "\n"

    def _build_ekrl_write_request(self, variable: str, value: Any) -> str:
        """构造Ethernet KRL XML写请求
        格式: <Robot><Write><Variable Name="xxx" Value="yyy"/></Write></Robot>
        """
        root = ET.Element("Robot")
        write_elem = ET.SubElement(root, "Write")
        ET.SubElement(write_elem, "Variable", Name=variable, Value=str(value))
        return ET.tostring(root, encoding="unicode") + "\n"

    def _parse_ekrl_response(self, xml_data: str) -> dict[str, Any]:
        """解析XML响应
        格式: <Robot><Read><Variable Name="xxx" Value="yyy"/>...</Read></Robot>
        """
        result = {}
        try:
            root = ET.fromstring(xml_data)
            for section in ("Read", "Write"):
                section_elem = root.find(section)
                if section_elem is None:
                    continue
                for var_elem in section_elem.findall("Variable"):
                    name = var_elem.get("Name", "")
                    value_str = var_elem.get("Value", "")
                    value = self._parse_value(value_str)
                    result[name] = value
        except ET.ParseError as e:
            logger.error("KUKA XML解析失败: %s - %s", xml_data[:200], e)
        return result

    @staticmethod
    def _parse_value(value_str: str) -> Any:
        """尝试将字符串值转为数值类型"""
        try:
            if "." in value_str:
                return float(value_str)
            return int(value_str)
        except (ValueError, TypeError):
            return value_str

    async def add_device(self, device_id: str, config: dict, points: list[dict] | None = None) -> None:
        """添加KUKA机器人设备，保存配置和XML变量映射"""
        if points is None:
            points = []
        self._devices[device_id] = {
            "config": config,
            "points": {p.get("name", p.get("address", "")): p for p in points if p.get("name") or p.get("address")},
        }
        logger.info("KUKA设备已添加: %s (%d测点)", device_id, len(points))

    async def discover_devices(self, config: dict) -> list[dict]:
        """扫描IP段发现KUKA机器人设备，通过尝试TCP连接EKRL端口判断设备是否在线

        config参数:
            network: 网段地址 (如 "192.168.1.0/24") 或单个IP
            ip: 单个IP地址 (与network二选一)
            port: EKRL端口 (默认54600)
            timeout: 连接超时秒数 (默认3)
            max_concurrent: 最大并发数 (默认10)
        """
        import ipaddress

        network = config.get("network", "")
        ip = config.get("ip", config.get("host", ""))
        port = int(config.get("port", DEFAULT_EKRL_PORT))
        timeout = float(config.get("timeout", 3.0))
        max_concurrent = int(config.get("max_concurrent", 10))

        if network:
            try:
                net = ipaddress.ip_network(network, strict=False)
                ips = [str(addr) for addr in net.hosts()]
            except ValueError as e:
                logger.error("KUKA发现: 无效的网段 %s - %s", network, e)
                return []
        elif ip:
            ips = [ip]
        else:
            logger.warning("KUKA发现: 未指定network或ip参数")
            return []

        discovered = []
        sem = asyncio.Semaphore(max_concurrent)

        async def _probe(ip_addr: str) -> dict | None:
            async with sem:
                try:
                    reader, writer = await asyncio.wait_for(
                        asyncio.open_connection(ip_addr, port),
                        timeout=timeout,
                    )
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except Exception:
                        pass
                    return {
                        "device_id": f"kuka_{ip_addr.replace('.', '_')}",
                        "name": f"KUKA Robot ({ip_addr})",
                        "protocol": "kuka_ekrl",
                        "config": {
                            "ip": ip_addr,
                            "port": port,
                        },
                        "points": [],
                    }
                except Exception:
                    return None

        tasks = [_probe(addr) for addr in ips]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for r in results:
            if isinstance(r, dict):
                discovered.append(r)

        logger.info("KUKA设备发现完成: 扫描%d个IP, 发现%d台设备", len(ips), len(discovered))
        return discovered

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        """读取KUKA机器人变量"""
        if not self._connected or not self._writer or not self._reader:
            return {}

        request_xml = self._build_ekrl_read_request(points)

        async with self._lock:
            try:
                self._writer.write(request_xml.encode("utf-8"))
                await self._writer.drain()

                buf = bytearray()
                deadline = asyncio.get_running_loop().time() + 5.0
                while True:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise TimeoutError()
                    chunk = await asyncio.wait_for(
                        self._reader.read(RECV_BUFFER_SIZE),
                        timeout=remaining,
                    )
                    if not chunk:
                        break
                    buf.extend(chunk)
                    text = buf.decode("utf-8", errors="replace")
                    if "</Robot>" in text or "</Response>" in text:
                        break

                if not buf:
                    return {}

                response_str = buf.decode("utf-8").strip()
                return self._parse_ekrl_response(response_str)

            except TimeoutError:
                logger.warning("KUKA读取超时: %s", device_id)
                self._connected = False
                return {}
            except Exception as e:
                logger.error("KUKA读取异常: %s - %s", device_id, e)
                self._connected = False
                return {}

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        """写入KUKA机器人变量"""
        if not self._connected or not self._writer or not self._reader:
            return False

        request_xml = self._build_ekrl_write_request(point, value)

        async with self._lock:
            try:
                self._writer.write(request_xml.encode("utf-8"))
                await self._writer.drain()

                response_data = await asyncio.wait_for(self._reader.read(RECV_BUFFER_SIZE), timeout=5.0)
                if not response_data:
                    return False

                response_str = response_data.decode("utf-8").strip()
                parsed = self._parse_ekrl_response(response_str)
                return point in parsed

            except TimeoutError:
                logger.warning("KUKA写入超时: %s", device_id)
                self._connected = False
                return False
            except Exception as e:
                logger.error("KUKA写入异常: %s - %s", device_id, e)
                self._connected = False
                return False

    def remove_device(self, device_id: str) -> None:
        """Remove a device at runtime"""
        self._health_stats.pop(device_id, None)
        self._offline_since.pop(device_id, None)
        logger.info("KUKA device removed: %s", device_id)
