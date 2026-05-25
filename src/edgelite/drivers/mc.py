"""三菱MC协议驱动 - 基于pymcprotocol库，支持iQ-R/iQ-Q系列PLC

支持：
- 三菱MC协议 (MELSEC Communication) TCP通信
- iQ-R/iQ-Q/L/FX系列PLC
- 批量读取优化 - 减少通信开销
- 位/字/浮点/32位多种数据类型
"""

from __future__ import annotations

import asyncio
import logging
import struct
from typing import Any

from edgelite.drivers.base import DriverPlugin

logger = logging.getLogger(__name__)


class McDriver(DriverPlugin):
    """三菱MC协议驱动

    配置参数:
        ip: PLC IP地址
        port: 端口号 (默认5007 for iQ-R, 5002 for Q series)
        plc_type: PLC型号 (默认"iQ-R")
    """

    plugin_name = "mitsubishi_mc"
    plugin_version = "1.1.0"
    supported_protocols = ["mc"]
    config_schema = {
        "description": "Mitsubishi MC protocol (MELSEC Communication), supports Q/L/FX series PLC",
        "fields": [
            {"name": "host", "type": "string", "label": "IP Address", "description": "PLC IP address", "default": "192.168.1.1", "required": True},
            {"name": "port", "type": "integer", "label": "Port", "description": "MC protocol port, default 5007", "default": 5007},
            {"name": "plc_type", "type": "string", "label": "PLC Type", "description": "Q series=Q, L series=L, FX series=iQ-R", "default": "Q", "options": ["Q", "L", "iQ-R"]},
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
        """启动MC驱动连接"""
        try:
            from pymcprotocol import Type3E
        except ImportError:
            raise ImportError("pymcprotocol未安装，请执行: pip install pymcprotocol>=0.3.0") from None

        self._config = config
        ip = config.get("host", "") or config.get("ip", "")
        port = int(config.get("port", 5007))
        plc_type = config.get("plc_type", "iQ-R")

        if not ip:
            raise ValueError("MC驱动配置缺少host参数")

        if not (1 <= port <= 65535):
            raise ValueError(f"MC驱动port超出范围[1-65535]，当前: {port}")

        try:
            self._client = Type3E(plctype=plc_type)
            await asyncio.to_thread(self._client.connect, ip, port)
            self._running = True
            logger.info("MC驱动连接成功: %s:%d (%s)", ip, port, plc_type)
        except Exception as e:
            logger.error("MC驱动连接失败: %s - %s", ip, e)
            raise

    async def stop(self) -> None:
        """停止MC驱动"""
        self._running = False
        if self._client:
            try:
                await asyncio.to_thread(self._client.close)
            except Exception as e:
                logger.warning("MC驱动断开异常: %s", e)
            self._client = None
        logger.info("MC驱动已停止")

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        """读取三菱PLC测点值

        测点地址格式: "D100" (数据寄存器), "M0" (内部继电器),
                      "X0" (输入), "Y0" (输出), "W0" (链接寄存器)
        字/位操作通过地址后缀区分:
            "D100" - 读取16位字
            "D100.0" - 读取位
            "D100.U" - 读取无符号16位
            "D100.L" - 读取32位长字
            "D100.F" - 读取浮点数

        支持批量读取优化，自动分组并发读取
        """
        if not self._running or not self._client:
            await self._try_reconnect(device_id)
            return {}

        batch_size = self._config.get("batch_size", 10)
        result = {}

        # 分批处理，每批batch_size个测点
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
            logger.warning("MC读取失败 %s: %s", address, e)
            return None

    def _read_point(self, address: str) -> Any:
        """同步读取单个测点"""
        # 解析地址
        addr, suffix = self._parse_address(address)

        if suffix == "bit":
            # 位读取
            return self._client.read_bit_device(addr, 1)[0]
        elif suffix == "word":
            # 字读取(16位有符号)
            values = self._client.read_device(addr, 1)
            return values[0]
        elif suffix == "uword":
            # 无符号字读取
            values = self._client.read_device(addr, 1)
            return values[0] & 0xFFFF
        elif suffix == "long":
            # 双字读取(32位)
            values = self._client.read_device(addr, 2)
            # FIXED: P2-2 原问题-high值读取失败但low继续执行，导致返回值不正确但无错误提示
            if not isinstance(values, (list, tuple)) or len(values) < 2:
                raise ValueError(f"32-bit read failed: expected 2 words, got {type(values)}")
            return (values[0] << 16) | (values[1] & 0xFFFF)
        elif suffix == "float":
            # 浮点数读取(32位)
            import struct

            values = self._client.read_device(addr, 2)
            # FIXED: P2-2 同上，浮点读取需要2个字
            if not isinstance(values, (list, tuple)) or len(values) < 2:
                raise ValueError(f"32-bit float read failed: expected 2 words, got {type(values)}")
            raw = struct.pack(">HH", values[0] & 0xFFFF, values[1] & 0xFFFF)
            return struct.unpack(">f", raw)[0]
        else:
            values = self._client.read_device(addr, 1)
            return values[0]

    def _parse_address(self, address: str) -> tuple[str, str]:
        """解析MC地址，返回(设备地址, 类型后缀)"""
        parts = address.split(".")
        addr = parts[0]

        if len(parts) > 1:
            bit_suffix = parts[1]
            if bit_suffix.isdigit():
                # 位偏移，如 D100.0
                return f"{addr}.{bit_suffix}", "bit"
            suffix_map = {
                "U": "uword",
                "L": "long",
                "F": "float",
            }
            return addr, suffix_map.get(bit_suffix.upper(), "word")

        # 根据设备类型判断默认读取方式
        device_type = addr[0].upper() if addr else ""
        if device_type in ("M", "X", "Y", "B", "F"):
            return addr, "bit"
        return addr, "word"

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        """写入三菱PLC测点值"""
        if not self._running or not self._client:
            await self._try_reconnect(device_id)
            return False

        try:
            async with self._lock:
                await asyncio.to_thread(self._write_point, point, value)
            return True
        except Exception as e:
            logger.error("MC写入失败 %s: %s", point, e)
            return False

    def _write_point(self, address: str, value: Any) -> None:
        """同步写入单个测点"""
        addr, suffix = self._parse_address(address)

        if suffix == "bit":
            self._client.write_bit_device(addr, [int(bool(value))])
        else:
            self._client.write_device(addr, [int(value)])

    async def _try_reconnect(self, device_id: str) -> None:
        if not self._config:
            return
        self._reconnect_count += 1
        if self._reconnect_count > self._MAX_RECONNECT_ATTEMPTS:
            logger.error("MC重连放弃: %s (已重试%d次)", device_id, self._reconnect_count)
            self._running = False
            return
        delay = min(self._reconnect_delay, self._RECONNECT_MAX_DELAY)
        logger.warning("MC连接断开，%.1fs后重连 (第%d次): %s", delay, self._reconnect_count, device_id)
        await asyncio.sleep(delay)
        self._reconnect_delay *= 2
        ip = self._config.get("host", "") or self._config.get("ip", "")
        port = int(self._config.get("port", 5007))
        plc_type = self._config.get("plc_type", "iQ-R")
        if not ip:
            return
        if self._client:
            try:
                await asyncio.to_thread(self._client.close)
            except Exception:
                pass
        try:
            from pymcprotocol import Type3E
            self._client = Type3E(plctype=plc_type)
            await asyncio.to_thread(self._client.connect, ip, port)
            self._running = True
            self._reconnect_count = 0
            self._reconnect_delay = self._RECONNECT_BASE_DELAY
            logger.info("MC重连成功: %s:%d", ip, port)
        except Exception as e:
            logger.error("MC重连失败: %s - %s", ip, e)

    async def add_device(self, device_id: str, config: dict, points: list[dict] | None = None) -> None:
        """添加MC协议设备，保存配置和测点映射"""
        if points is None:
            points = []
        self._devices[device_id] = {
            "config": config,
            "points": {p.get("name", p.get("address", "")): p for p in points if p.get("name") or p.get("address")},
        }
        logger.info("MC设备已添加: %s (%d测点)", device_id, len(points))

    async def discover_devices(self, config: dict) -> list[dict]:
        """扫描IP段发现三菱MC协议设备，通过尝试连接测试判断设备是否在线

        config参数:
            network: 网段地址 (如 "192.168.1.0/24") 或单个IP
            host: 单个IP地址 (与network二选一)
            port: MC协议端口 (默认5007)
            plc_type: PLC型号 (默认"iQ-R")
            timeout: 连接超时秒数 (默认3)
            max_concurrent: 最大并发数 (默认10)
        """
        try:
            from pymcprotocol import Type3E
        except ImportError:
            logger.warning("pymcprotocol未安装，无法执行MC设备发现")
            return []

        import ipaddress

        network = config.get("network", "")
        host = config.get("host", config.get("ip", ""))
        port = int(config.get("port", 5007))
        plc_type = config.get("plc_type", "iQ-R")
        timeout = int(config.get("timeout", 3))
        max_concurrent = int(config.get("max_concurrent", 10))

        if network:
            try:
                net = ipaddress.ip_network(network, strict=False)
                ips = [str(ip) for ip in net.hosts()]
            except ValueError as e:
                logger.error("MC发现: 无效的网段 %s - %s", network, e)
                return []
        elif host:
            ips = [host]
        else:
            logger.warning("MC发现: 未指定network或host参数")
            return []

        discovered = []
        sem = asyncio.Semaphore(max_concurrent)

        async def _probe(ip_addr: str) -> dict | None:
            async with sem:
                try:
                    client = Type3E(plctype=plc_type)
                    await asyncio.wait_for(
                        asyncio.to_thread(client.connect, ip_addr, port),
                        timeout=timeout + 1,
                    )
                    try:
                        await asyncio.to_thread(client.close)
                    except Exception:
                        pass
                    return {
                        "device_id": f"mc_{ip_addr.replace('.', '_')}",
                        "name": f"Mitsubishi PLC ({ip_addr})",
                        "protocol": "mc",
                        "config": {
                            "host": ip_addr,
                            "port": port,
                            "plc_type": plc_type,
                        },
                        "points": [],
                    }
                except Exception:
                    return None

        tasks = [_probe(ip) for ip in ips]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for r in results:
            if isinstance(r, dict):
                discovered.append(r)

        logger.info("MC设备发现完成: 扫描%d个IP, 发现%d台设备", len(ips), len(discovered))
        return discovered

    def remove_device(self, device_id: str) -> None:
        """Remove a device at runtime"""
        self._health_stats.pop(device_id, None)
        self._offline_since.pop(device_id, None)
        logger.info("MC device removed: %s", device_id)
