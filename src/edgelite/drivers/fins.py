"""欧姆龙FINS协议驱动 - 基于fins库(TCPFinsConnection)，支持CJ/CP/NJ系列PLC

支持：
- Omron FINS协议 TCP/UDP通信
- CJ/CP/NJ系列PLC
- 批量读取优化 - 减少通信开销
- D/CIO/W/H多区域支持
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from edgelite.drivers.base import DriverPlugin

logger = logging.getLogger(__name__)

_AREA_MAP = {
    "D": "d",
    "DM": "d",
    "CIO": "c",
    "C": "c",
    "W": "w",
    "WR": "w",
    "H": "h",
    "HR": "h",
    "A": "h",
    "AR": "h",
}

_DTYPE_MAP = {
    1: "b",
    16: "w",
    32: "r",
}


class OmronFinsDriver(DriverPlugin):
    """欧姆龙FINS协议驱动

    配置参数:
        host: PLC IP地址
        port: FINS TCP端口 (默认9600)
        transport: 传输模式 tcp/udp (默认tcp)
    """

    plugin_name = "omron_fins"
    plugin_version = "2.1.0"
    supported_protocols = ["fins"]
    config_schema = {
        "description": "Omron FINS protocol, supports CJ/CP/NJ series PLC",
        "fields": [
            {"name": "host", "type": "string", "label": "IP Address", "description": "PLC IP address", "default": "192.168.1.1", "required": True},
            {"name": "port", "type": "integer", "label": "Port", "description": "FINS TCP port, default 9600", "default": 9600},
            {"name": "transport", "type": "string", "label": "Transport", "description": "Transport mode: tcp or udp", "default": "tcp", "options": ["tcp", "udp"]},
            {"name": "batch_size", "type": "integer", "label": "Batch Size", "description": "Number of points to read in parallel (default 10)", "default": 10},
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
        try:
            from fins.tcp import TCPFinsConnection
        except ImportError:
            raise ImportError("fins未安装，请执行: pip install fins") from None

        self._config = config
        ip = config.get("host", "") or config.get("ip", "")
        port = int(config.get("port", 9600))

        if not ip:
            raise ValueError("FINS驱动配置缺少host参数")

        if not (1 <= port <= 65535):
            raise ValueError(f"FINS驱动port超出范围[1-65535]，当前: {port}")

        try:
            self._client = TCPFinsConnection()
            await asyncio.to_thread(self._client.connect, ip, port)
            self._running = True
            logger.info("FINS驱动连接成功: %s:%d", ip, port)
        except Exception as e:
            logger.error("FINS驱动连接失败: %s - %s", ip, e)
            raise

    async def stop(self) -> None:
        self._running = False
        if self._client:
            try:
                await asyncio.to_thread(self._client.fins_socket.close)
            except Exception as e:
                logger.warning("FINS驱动断开异常: %s", e)
            self._client = None
        logger.info("FINS驱动已停止")

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        """读取欧姆龙PLC测点值

        测点地址格式:
            "D100" - DM区字地址100
            "CIO100" - CIO区字地址100
            "W100" - 工作区字地址100
            "D100,r" - DM区浮点读取
            "D100,i" - DM区有符号整数读取
            "D100,b" - DM区位读取

        支持批量读取优化，自动并发读取多个测点
        """
        if not self._running or not self._client:
            await self._try_reconnect(device_id)
            return {}

        batch_size = self._config.get("batch_size", 10)
        result = {}

        # 分批处理
        for i in range(0, len(points), batch_size):
            batch = points[i:i + batch_size]
            batch_result = await self._read_points_batch(batch)
            result.update(batch_result)

        return result

    async def _read_points_batch(self, points: list[str]) -> dict[str, Any]:
        """批量读取多个测点（并发请求）"""
        tasks = [self._read_point_async(p) for p in points]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return {p: r if not isinstance(r, Exception) else None for p, r in zip(points, results)}

    async def _read_point_async(self, address: str) -> Any:
        """异步读取单个测点"""
        try:
            return await asyncio.to_thread(self._read_point, address)
        except Exception as e:
            logger.warning("FINS读取失败 %s: %s", address, e)
            return None

    def _parse_address(self, address: str) -> tuple[str, int, str]:
        addr_upper = address.upper()
        data_type = "w"

        if "," in address:
            parts = address.split(",")
            addr_part = parts[0].strip()
            if len(parts) > 1:
                dt = parts[1].strip().lower()
                if dt in ("b", "w", "i", "r", "ui", "dw", "str"):
                    data_type = dt
            addr_upper = addr_part.upper()
            address = addr_part

        if addr_upper.startswith("CIO"):
            offset = int(addr_upper[3:])
            return ("c", offset, data_type)
        elif addr_upper.startswith("C"):
            offset = int(addr_upper[1:])
            return ("c", offset, data_type)
        elif addr_upper.startswith("D"):
            offset = int(addr_upper[1:])
            return ("d", offset, data_type)
        elif addr_upper.startswith("W"):
            offset = int(addr_upper[1:])
            return ("w", offset, data_type)
        elif addr_upper.startswith("H"):
            offset = int(addr_upper[1:])
            return ("h", offset, data_type)
        elif addr_upper.startswith("A"):
            offset = int(addr_upper[1:])
            return ("h", offset, data_type)

        raise ValueError(f"无效的FINS地址: {address}")

    def _read_point(self, address: str) -> Any:
        area, offset, data_type = self._parse_address(address)
        return self._client.read(area, offset, data_type=data_type, number_of_values=1)

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        if not self._running or not self._client:
            await self._try_reconnect(device_id)
            return False

        try:
            async with self._lock:
                area, offset, data_type = self._parse_address(point)
                await asyncio.to_thread(self._client.write, value, area, offset, data_type)
            return True
        except Exception as e:
            logger.error("FINS写入失败 %s: %s", point, e)
            return False

    async def _try_reconnect(self, device_id: str) -> None:
        if not self._config:
            return
        self._reconnect_count += 1
        if self._reconnect_count > self._MAX_RECONNECT_ATTEMPTS:
            logger.error("FINS重连放弃: %s (已重试%d次)", device_id, self._reconnect_count)
            self._running = False
            return
        delay = min(self._reconnect_delay, self._RECONNECT_MAX_DELAY)
        logger.warning("FINS连接断开，%.1fs后重连 (第%d次): %s", delay, self._reconnect_count, device_id)
        await asyncio.sleep(delay)
        self._reconnect_delay *= 2
        ip = self._config.get("host", "") or self._config.get("ip", "")
        port = int(self._config.get("port", 9600))
        if not ip:
            return
        if self._client:
            try:
                await asyncio.to_thread(self._client.fins_socket.close)
            except Exception:
                pass
        try:
            from fins.tcp import TCPFinsConnection
            self._client = TCPFinsConnection()
            await asyncio.to_thread(self._client.connect, ip, port)
            self._running = True
            self._reconnect_count = 0
            self._reconnect_delay = self._RECONNECT_BASE_DELAY
            logger.info("FINS重连成功: %s:%d", ip, port)
        except Exception as e:
            logger.error("FINS重连失败: %s - %s", ip, e)

    async def add_device(self, device_id: str, config: dict, points: list[dict] | None = None) -> None:
        """添加FINS设备，保存配置和测点映射"""
        if points is None:
            points = []
        self._devices[device_id] = {
            "config": config,
            "points": {p.get("name", p.get("address", "")): p for p in points if p.get("name") or p.get("address")},
        }
        logger.info("FINS设备已添加: %s (%d测点)", device_id, len(points))

    async def discover_devices(self, config: dict) -> list[dict]:
        """通过FINS UDP广播发现欧姆龙PLC设备

        FINS协议支持UDP广播发现：向广播地址发送FINS控制器搜索帧，
        在线设备会响应其IP、节点号等信息。

        config参数:
            broadcast: 广播地址 (默认 "255.255.255.255")
            port: FINS UDP端口 (默认9600)
            timeout: 等待响应超时秒数 (默认3)
            source_node: 本地FINS节点号 (默认0)
        """
        import socket

        broadcast = config.get("broadcast", "255.255.255.255")
        port = int(config.get("port", 9600))
        timeout = float(config.get("timeout", 3.0))
        source_node = int(config.get("source_node", 0))

        discovered = []
        sock = None

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.settimeout(timeout)

            # FINS控制器搜索帧:
            # ICF(0x80) RSV(0x00) GCT(0x02) DNA(0x00) DA1(0x00) DA2(0x00)
            # SNA(0x00) SA1(source_node) SA2(0x00) SID(0x00)
            # MRC(0x05) SRC(0x01) - 控制器搜索命令
            fins_search_frame = bytes([
                0x80, 0x00, 0x02,       # ICF, RSV, GCT
                0x00, 0x00, 0x00,       # DNA, DA1, DA2 (目标: 网络0, 节点0)
                0x00, source_node, 0x00, # SNA, SA1, SA2 (源: 网络0, 本地节点)
                0x00,                    # SID
                0x05, 0x01,              # MRC=0x05(控制器), SRC=0x01(搜索)
            ])

            await asyncio.to_thread(sock.sendto, fins_search_frame, (broadcast, port))

            # 收集响应
            deadline = asyncio.get_running_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    data, addr = await asyncio.wait_for(
                        asyncio.to_thread(sock.recvfrom, 4096),
                        timeout=remaining,
                    )
                except (socket.timeout, TimeoutError):
                    break
                except Exception:
                    break

                if len(data) < 14:
                    continue

                try:
                    # 解析FINS响应帧
                    # 偏移: ICF(0) RSV(1) GCT(2) DNA(3) DA1(4) DA2(5)
                    #       SNA(6) SA1(7) SA2(8) SID(9) MRC(10) SRC(11)
                    remote_node = data[7]  # SA1: 响应设备的FINS节点号
                    remote_network = data[6]  # SNA: 响应设备的网络号
                    mrc = data[10]
                    src = data[11]

                    # MRC=0x05, SRC=0x01 是控制器搜索响应
                    if mrc != 0x05 or src != 0x01:
                        continue

                    # 尝试解析控制器名称 (如果有)
                    controller_name = ""
                    if len(data) > 14:
                        try:
                            controller_name = data[14:].decode("ascii", errors="replace").strip("\x00")
                        except Exception:
                            pass

                    ip_addr = addr[0]
                    device_info = {
                        "device_id": f"fins_{ip_addr.replace('.', '_')}",
                        "name": f"Omron PLC ({ip_addr})" + (f" - {controller_name}" if controller_name else ""),
                        "protocol": "fins",
                        "config": {
                            "host": ip_addr,
                            "port": port,
                            "transport": "udp",
                        },
                        "points": [],
                        "details": {
                            "fins_node": remote_node,
                            "fins_network": remote_network,
                            "controller_name": controller_name,
                        },
                    }
                    # 去重
                    if not any(d["device_id"] == device_info["device_id"] for d in discovered):
                        discovered.append(device_info)
                except Exception as e:
                    logger.debug("FINS发现: 解析响应失败 - %s", e)
                    continue

        except Exception as e:
            logger.error("FINS设备发现失败: %s", e)
        finally:
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass

        logger.info("FINS设备发现完成: 发现%d台设备", len(discovered))
        return discovered

    def remove_device(self, device_id: str) -> None:
        """Remove a device at runtime"""
        self._health_stats.pop(device_id, None)
        self._offline_since.pop(device_id, None)
        logger.info("FINS device removed: %s", device_id)
