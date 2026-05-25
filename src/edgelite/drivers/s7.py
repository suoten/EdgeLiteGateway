"""西门子S7协议驱动 - 基于snap7库，支持S7-200/300/400/1200/1500"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from edgelite.drivers.base import DriverPlugin

logger = logging.getLogger(__name__)


class S7Driver(DriverPlugin):
    """西门子S7协议驱动

    配置参数:
        ip: PLC IP地址
        rack: 机架号 (默认0)
        slot: 插槽号 (默认1，S7-1200/1500默认1，S7-300默认2)
        db_number: 数据块编号
        heartbeat_interval: 心跳检测间隔秒数 (默认30)
        pdu_size: 期望PDU大小 (默认0=自动协商)

    常见PLC型号rack/slot配置参考:
        S7-200 Smart: rack=0, slot=0  (通过以太网扩展)
        S7-300:       rack=0, slot=2  (CPU在slot 2)
        S7-400:       rack=0, slot=2  (CPU在slot 2)
        S7-1200:      rack=0, slot=1  (CPU在slot 1)
        S7-1500:      rack=0, slot=1  (CPU在slot 1)
    """

    plugin_name = "siemens_s7"
    plugin_version = "1.0.0"
    supported_protocols = ["s7"]
    config_schema = {
        "description": "Siemens S7 PLC protocol (S7-200 Smart/300/400/1200/1500)",
        "fields": [
            {"name": "ip", "type": "string", "label": "IP Address", "description": "PLC IP address", "default": "192.168.1.1", "required": True},
            {"name": "port", "type": "integer", "label": "Port", "description": "S7 TCP port (default 102, ISO-on-TCP)", "default": 102},
            {"name": "rack", "type": "integer", "label": "Rack", "description": "Hardware rack number (0-7). S7-200 Smart/300/400/1200/1500 usually 0", "default": 0},
            {"name": "slot", "type": "integer", "label": "Slot", "description": "CPU slot number (0-31). S7-200 Smart: 0, S7-300: 2, S7-1200/1500: 1", "default": 1},
            {"name": "connect_timeout", "type": "integer", "label": "Connection Timeout (s)", "description": "TCP connection timeout in seconds (default 30, increase for remote/cloud connections)", "default": 30},
            {"name": "heartbeat_interval", "type": "integer", "label": "Heartbeat Interval (s)", "description": "Seconds between heartbeat checks (default 30)", "default": 30},
            {"name": "pdu_size", "type": "integer", "label": "PDU Size", "description": "Desired PDU size in bytes (0=auto-negotiate, default 0)", "default": 0},
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
        self._pdu_size: int = 240  # 默认PDU大小(S7-300)，连接后协商更新
        self._heartbeat_interval: int = 30  # 心跳检测间隔秒数
        self._heartbeat_task: asyncio.Task | None = None

    _DEFAULT_CONNECT_TIMEOUT = 30  # seconds — matches Modbus/_CONNECTION_TIMEOUT in ProtoForge S7Server

    async def _s7_connect_with_timeout(self, client, ip: str, rack: int, slot: int) -> None:
        """使用可配置超时的 S7 连接（避免 snap7 C 库硬编码的 10s 超时导致公网连接失败）。

        snap7 C 库内部默认 TCP 连接超时为 10s，在公网/高延迟环境下容易触发。
        这里通过在 asyncio.to_thread 中先用 set_connection_params() 设置超时，再调用 connect()。
        """
        import snap7
        # set_connection_params(ip, localtsap, remotetsap, timeout) — 超时单位为秒
        timeout = self._config.get("connect_timeout", self._DEFAULT_CONNECT_TIMEOUT)
        client.set_connection_params(ip, 0, 0, timeout)
        await asyncio.to_thread(client.connect, ip, rack, slot)

    async def start(self, config: dict) -> None:
        """启动S7驱动连接"""
        try:
            import snap7
        except ImportError:
            raise ImportError(
                "snap7未安装，请执行: pip install python-snap7。"
                "同时需要下载snap7动态库: https://snap7.sourceforge.net/"
            ) from None

        self._config = config
        ip = config.get("ip", "")
        port = config.get("port", 102)
        try:
            rack = int(config.get("rack", 0))
            slot = int(config.get("slot", 1))
        except (ValueError, TypeError) as e:
            raise ValueError(f"Invalid S7 rack/slot config value: {e}") from e

        if not ip:
            raise ValueError("S7 driver config missing 'ip' parameter")

        if not (0 <= rack <= 7):
            raise ValueError(
                f"S7 rack out of range [0-7], got {rack}. "
                f"Common: S7-300/400 rack=0, S7-1200/1500 rack=0"
            )
        if not (0 <= slot <= 31):
            raise ValueError(
                f"S7 slot out of range [0-31], got {slot}. "
                f"Common: S7-300 slot=2, S7-1200/1500 slot=1"
            )

        self._heartbeat_interval = int(config.get("heartbeat_interval", 30))

        try:
            self._client = snap7.client.Client()
            await self._s7_connect_with_timeout(self._client, ip, rack, slot)
            self._running = True
            self._reconnect_count = 0
            self._reconnect_delay = self._RECONNECT_BASE_DELAY

            # PDU大小协商
            self._pdu_size = await self._get_pdu_size()
            logger.info(
                "[s7] device=%s code=CONN_OK msg=Connected (rack=%d, slot=%d, pdu=%d)",
                ip, rack, slot, self._pdu_size,
            )

            # 启动心跳检测
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        except Exception as e:
            self._log_error(ip, "CONN_FAILED", f"msg=Connection failed (rack={rack}, slot={slot}) - {e}")
            raise

    async def stop(self) -> None:
        """停止S7驱动"""
        self._running = False
        # 取消心跳任务
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None
        if self._client:
            try:
                await asyncio.to_thread(self._client.disconnect)
            except Exception as e:
                logger.warning("S7驱动断开异常: %s", e)
            self._client = None
        logger.info("[s7] code=STOPPED msg=Driver stopped")

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        """读取S7 PLC测点值

        测点地址格式: "DB1.X0.0" (数据块.类型偏移.位偏移)
        支持的类型前缀:
            X - 位(BOOL)
            B - 字节(INT8)
            W - 字(INT16)
            D - 双字(INT32/FLOAT)
            R - 实数(FLOAT32)
        """
        if not self._running or not self._client:
            await self._try_reconnect(device_id)
            return {}

        result = {}
        async with self._lock:
            try:
                values = await asyncio.to_thread(self._read_points_batch, points)
                result = values
            except Exception as e:
                self._log_error(device_id, "READ_ERROR", f"msg=Batch read failed, fallback to per-point - {e}")
                if not self._is_connected():
                    await self._try_reconnect(device_id)
                    return {}
                for point_addr in points:
                    try:
                        value = await asyncio.to_thread(self._read_point, point_addr)
                        result[point_addr] = value
                    except Exception as e2:
                        logger.warning("[s7] device=%s code=READ_ERROR msg=Point read failed %s - %s", device_id, point_addr, e2)
                        result[point_addr] = None

        return result

    def _read_points_batch(self, addresses: list[str]) -> dict[str, Any]:
        """同步批量读取多个测点（优化DB访问，合并相邻地址为一次读取）"""
        result = {}
        if not addresses:
            return result

        # 尝试使用优化DB读取
        try:
            segments = self._optimize_db_reads(addresses)
            for db_number, start_offset, total_bytes, items in segments:
                try:
                    data = self._client.db_read(db_number, start_offset, total_bytes)
                    for addr, rel_offset, size, type_char, bit_offset in items:
                        try:
                            result[addr] = self._extract_value(data, rel_offset, size, type_char, bit_offset)
                        except Exception as e:
                            logger.warning("[s7] code=READ_ERROR msg=Extract value failed %s - %s", addr, e)
                            result[addr] = None
                except Exception as e:
                    logger.warning("[s7] code=READ_ERROR msg=DB read failed DB%d.%d+%d - %s", db_number, start_offset, total_bytes, e)
                    # 该段内所有测点标记为失败
                    for addr, _, _, _, _ in items:
                        result[addr] = None
            return result
        except Exception:
            # 优化读取失败，退回逐点读取
            for addr in addresses:
                try:
                    result[addr] = self._read_point(addr)
                except Exception as e:
                    logger.warning("[s7] code=READ_ERROR msg=Point read failed %s - %s", addr, e)
                    result[addr] = None
            return result

    def _parse_address(self, address: str) -> tuple[int, str, int, int, int]:
        """解析S7地址，返回 (db_number, type_char, byte_offset, bit_offset, size_bytes)"""
        parts = address.split(".")
        if len(parts) < 2 or not parts[0].startswith("DB"):
            raise ValueError(f"Invalid S7 address format: {address}, expected DBN.TB")

        try:
            db_number = int(parts[0][2:])
        except (ValueError, IndexError) as e:
            raise ValueError(f"Invalid S7 DB number in address: {address}") from e
        if len(parts) < 2 or not parts[1]:
            raise ValueError(f"Invalid S7 type/offset in address: {address}")
        type_char = parts[1][0].upper()
        try:
            byte_offset = int(parts[1][1:])
        except ValueError as e:
            raise ValueError(f"Invalid S7 byte offset in address: {address}") from e
        bit_offset = int(parts[2]) if len(parts) > 2 else 0

        # 根据类型确定读取字节数
        size_map = {"X": 1, "B": 1, "W": 2, "D": 4, "R": 4}
        size = size_map.get(type_char)
        if size is None:
            raise ValueError(f"Unsupported S7 data type: {type_char}")

        return db_number, type_char, byte_offset, bit_offset, size

    def _optimize_db_reads(self, addresses: list[str]) -> list[tuple[int, int, int, list[tuple[str, int, int, str, int]]]]:
        """将地址列表优化为DB读取段

        合并同一DB中连续/重叠地址范围，每个读取段不超过 _pdu_size - 12 字节。

        Returns: [(db_number, start_offset, total_bytes, [(addr_str, offset_in_data, size_bytes, type_char, bit_offset)])]
        """
        # 解析所有地址
        parsed = []
        for addr in addresses:
            db, type_char, byte_offset, bit_offset, size = self._parse_address(addr)
            parsed.append((db, byte_offset, size, addr, type_char, bit_offset))

        # 按DB号分组，每组内按偏移排序
        by_db: dict[int, list[tuple[int, int, str, str, int]]] = {}
        for db, offset, size, addr, type_char, bit_offset in parsed:
            by_db.setdefault(db, []).append((offset, size, addr, type_char, bit_offset))

        max_segment_bytes = self._pdu_size - 12  # S7读取请求头12字节

        # 合并连续范围并按PDU大小分包
        segments: list[tuple[int, int, int, list[tuple[str, int, int, str, int]]]] = []
        for db, items in by_db.items():
            items.sort()
            seg_start = items[0][0]
            seg_end = seg_start + items[0][1]
            seg_items: list[tuple[str, int, int, str, int]] = [(items[0][2], 0, items[0][1], items[0][3], items[0][4])]

            for offset, size, addr, type_char, bit_offset in items[1:]:
                new_end = max(seg_end, offset + size)
                # 允许4字节间隔合并，且不超过PDU限制
                if offset <= seg_end + 4 and (new_end - seg_start) <= max_segment_bytes:
                    rel_offset = offset - seg_start
                    seg_items.append((addr, rel_offset, size, type_char, bit_offset))
                    seg_end = new_end
                else:
                    # 保存当前段
                    segments.append((db, seg_start, seg_end - seg_start, seg_items))
                    # 开始新段
                    seg_start = offset
                    seg_end = offset + size
                    seg_items = [(addr, 0, size, type_char, bit_offset)]

            segments.append((db, seg_start, seg_end - seg_start, seg_items))

        return segments

    @staticmethod
    def _extract_value(data: bytearray, offset: int, size: int, type_char: str, bit_offset: int) -> Any:
        """从读取的数据中提取测点值"""
        import struct

        if type_char == "X":
            return bool(data[offset] & (1 << bit_offset))
        elif type_char == "B":
            return int.from_bytes(data[offset:offset + 1], byteorder="big", signed=True)
        elif type_char == "W":
            return int.from_bytes(data[offset:offset + 2], byteorder="big", signed=True)
        elif type_char == "D":
            return int.from_bytes(data[offset:offset + 4], byteorder="big", signed=True)
        elif type_char == "R":
            return struct.unpack(">f", data[offset:offset + 4])[0]
        else:
            raise ValueError(f"Unsupported S7 data type: {type_char}")

    def _read_point(self, address: str) -> Any:
        """同步读取单个测点（在线程池中执行）"""
        parts = address.split(".")
        if len(parts) < 2 or not parts[0].startswith("DB"):
            raise ValueError(f"Invalid S7 address format: {address}, expected DBN.TB")

        try:  # FIXED: 原问题-parts[0][2:]/parts[1]硬索引，格式错误时IndexError/ValueError
            db_number = int(parts[0][2:])
        except (ValueError, IndexError) as e:
            raise ValueError(f"Invalid S7 DB number in address: {address}") from e
        if len(parts) < 2 or not parts[1]:
            raise ValueError(f"Invalid S7 type/offset in address: {address}")
        type_char = parts[1][0].upper()
        try:
            byte_offset = int(parts[1][1:])
        except ValueError as e:
            raise ValueError(f"Invalid S7 byte offset in address: {address}") from e
        bit_offset = int(parts[2]) if len(parts) > 2 else 0

        if type_char == "X":
            # 读取位(BOOL)
            data = self._client.db_read(db_number, byte_offset, 1)
            return bool(data[0] & (1 << bit_offset))
        elif type_char == "B":
            # 读取字节
            data = self._client.db_read(db_number, byte_offset, 1)
            return int.from_bytes(data, byteorder="big", signed=True)
        elif type_char == "W":
            # 读取字(INT16)
            data = self._client.db_read(db_number, byte_offset, 2)
            return int.from_bytes(data, byteorder="big", signed=True)
        elif type_char == "D":
            # 读取双字(INT32)
            data = self._client.db_read(db_number, byte_offset, 4)
            return int.from_bytes(data, byteorder="big", signed=True)
        elif type_char == "R":
            # 读取实数(FLOAT32)
            data = self._client.db_read(db_number, byte_offset, 4)
            import struct

            return struct.unpack(">f", data)[0]
        else:
            raise ValueError(f"不支持的S7数据类型: {type_char}")

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        """写入S7 PLC测点值"""
        if not self._running or not self._client:
            await self._try_reconnect(device_id)
            return False

        try:
            async with self._lock:
                await asyncio.to_thread(self._write_point, point, value)
            return True
        except Exception as e:
            self._log_error(device_id, "WRITE_ERROR", f"msg=Write failed {point} - {e}")
            if not self._is_connected():
                await self._try_reconnect(device_id)
            return False

    def _write_point(self, address: str, value: Any) -> None:
        """同步写入单个测点"""
        parts = address.split(".")
        try:  # FIXED: 原问题-_write_point同样存在硬索引问题
            db_number = int(parts[0][2:])
        except (ValueError, IndexError) as e:
            raise ValueError(f"Invalid S7 DB number in address: {address}") from e
        if len(parts) < 2 or not parts[1]:
            raise ValueError(f"Invalid S7 type/offset in address: {address}")
        type_char = parts[1][0].upper()
        try:
            byte_offset = int(parts[1][1:])
        except ValueError as e:
            raise ValueError(f"Invalid S7 byte offset in address: {address}") from e
        bit_offset = int(parts[2]) if len(parts) > 2 else 0

        if type_char == "X":
            data = self._client.db_read(db_number, byte_offset, 1)
            if value:
                data[0] |= 1 << bit_offset
            else:
                data[0] &= ~(1 << bit_offset)
            self._client.db_write(db_number, byte_offset, data)
        elif type_char == "W":
            data = value.to_bytes(2, byteorder="big", signed=True)
            self._client.db_write(db_number, byte_offset, data)
        elif type_char == "D":
            data = value.to_bytes(4, byteorder="big", signed=True)
            self._client.db_write(db_number, byte_offset, data)
        elif type_char == "R":
            import struct

            data = struct.pack(">f", float(value))
            self._client.db_write(db_number, byte_offset, data)

    async def add_device(self, device_id: str, config: dict, points: list[dict] | None = None) -> None:
        """添加S7设备，保存配置和测点映射"""
        if points is None:
            points = []
        self._devices[device_id] = {
            "config": config,
            "points": {p.get("name", p.get("address", "")): p for p in points if p.get("name") or p.get("address")},
        }
        logger.info("S7设备已添加: %s (%d测点)", device_id, len(points))

    async def discover_devices(self, config: dict) -> list[dict]:
        """扫描IP段发现S7设备，通过尝试S7连接测试判断设备是否在线

        config参数:
            network: 网段地址 (如 "192.168.1.0/24") 或单个IP
            host: 单个IP地址 (与network二选一)
            port: S7端口 (默认102)
            rack: 机架号 (默认0)
            slot: 插槽号 (默认1)
            timeout: 连接超时秒数 (默认3)
            max_concurrent: 最大并发数 (默认10)
        """
        try:
            import snap7
        except ImportError:
            logger.warning("snap7未安装，无法执行S7设备发现")
            return []

        import ipaddress

        network = config.get("network", "")
        host = config.get("host", config.get("ip", ""))
        port = int(config.get("port", 102))
        rack = int(config.get("rack", 0))
        slot = int(config.get("slot", 1))
        timeout = int(config.get("timeout", 3))
        max_concurrent = int(config.get("max_concurrent", 10))

        # 确定要扫描的IP列表
        if network:
            try:
                net = ipaddress.ip_network(network, strict=False)
                ips = [str(ip) for ip in net.hosts()]
            except ValueError as e:
                logger.error("S7发现: 无效的网段 %s - %s", network, e)
                return []
        elif host:
            ips = [host]
        else:
            logger.warning("S7发现: 未指定network或host参数")
            return []

        discovered = []
        sem = asyncio.Semaphore(max_concurrent)

        async def _probe(ip_addr: str) -> dict | None:
            async with sem:
                try:
                    client = snap7.client.Client()
                    client.set_connection_params(ip_addr, 0, 0, timeout)
                    await asyncio.wait_for(
                        asyncio.to_thread(client.connect, ip_addr, rack, slot),
                        timeout=timeout + 1,
                    )
                    try:
                        info = await asyncio.to_thread(client.get_cpu_info)
                        model = info.ModuleName if hasattr(info, "ModuleName") else ""
                        serial = info.SerialNumber if hasattr(info, "SerialNumber") else ""
                    except Exception:
                        model = ""
                        serial = ""
                    try:
                        await asyncio.to_thread(client.disconnect)
                    except Exception:
                        pass
                    return {
                        "device_id": f"s7_{ip_addr.replace('.', '_')}",
                        "name": f"S7 PLC ({ip_addr})" + (f" - {model}" if model else ""),
                        "protocol": "s7",
                        "config": {
                            "ip": ip_addr,
                            "port": port,
                            "rack": rack,
                            "slot": slot,
                        },
                        "points": [],
                        "details": {
                            "model": model,
                            "serial": serial,
                        },
                    }
                except Exception:
                    return None

        tasks = [_probe(ip) for ip in ips]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for r in results:
            if isinstance(r, dict):
                discovered.append(r)

        logger.info("S7设备发现完成: 扫描%d个IP, 发现%d台设备", len(ips), len(discovered))
        return discovered

    def remove_device(self, device_id: str) -> None:
        """Remove a device at runtime"""
        self._health_stats.pop(device_id, None)
        self._offline_since.pop(device_id, None)
        logger.info("S7 device removed: %s", device_id)

    def _is_connected(self) -> bool:
        """检查S7客户端连接状态"""
        if not self._client:
            return False
        try:
            return self._client.get_connected()
        except Exception:
            return False

    async def _get_pdu_size(self) -> int:
        """获取协商后的PDU大小"""
        try:
            pdu = await asyncio.to_thread(self._client.get_pdu_size)
            if pdu and pdu > 0:
                return pdu
        except Exception:
            self._log_error(self._config.get("ip", "unknown"), "PDU_NEGOTIATION_FAILED", "msg=Failed to get PDU size, using default 240")
        return 240  # 默认240字节(S7-300)

    async def _heartbeat_loop(self) -> None:
        """定期心跳检测连接状态"""
        device_id = self._config.get("ip", "unknown")
        while self._running:
            await asyncio.sleep(self._heartbeat_interval)
            if not self._running:
                break
            if not self._is_connected():
                self._log_error(device_id, "HEARTBEAT_FAILED", "msg=Connection lost, triggering reconnect")
                await self._try_reconnect(device_id)

    def _log_error(self, device_id: str, error_code: str, message: str) -> None:
        """统一日志四元组格式

        格式: [s7] device={device_id} code={error_code} {message}
        error_code: CONN_FAILED, READ_ERROR, WRITE_ERROR, PDU_NEGOTIATION_FAILED,
                    HEARTBEAT_FAILED, RECONNECT_OK, RECONNECT_FAILED
        """
        logger.error("[s7] device=%s code=%s %s", device_id, error_code, message)

    async def _try_reconnect(self, device_id: str) -> None:
        """指数退避重连：初始1秒，最大60秒，每次翻倍，最多100次"""
        if not self._config:
            return

        self._reconnect_count += 1
        if self._reconnect_count > self._MAX_RECONNECT_ATTEMPTS:
            self._log_error(device_id, "RECONNECT_FAILED", f"msg=Gave up after {self._reconnect_count} attempts, device offline")
            self._running = False
            return

        delay = min(self._reconnect_delay, self._RECONNECT_MAX_DELAY)
        logger.warning(
            "[s7] device=%s code=RECONNECTING msg=Connection lost, retry in %.1fs (attempt #%d)",
            device_id, delay, self._reconnect_count,
        )
        await asyncio.sleep(delay)
        self._reconnect_delay *= 2

        ip = self._config.get("ip", "")
        try:
            rack = int(self._config.get("rack", 0))
            slot = int(self._config.get("slot", 1))
        except (ValueError, TypeError) as e:
            self._log_error(device_id, "RECONNECT_FAILED", f"msg=Invalid rack/slot config - {e}")
            return

        try:
            import snap7
        except ImportError:
            return

        if self._client:
            try:
                await asyncio.to_thread(self._client.disconnect)
            except Exception:
                pass

        try:
            self._client = snap7.client.Client()
            await self._s7_connect_with_timeout(self._client, ip, rack, slot)
            self._running = True
            self._reconnect_count = 0
            self._reconnect_delay = self._RECONNECT_BASE_DELAY
            # 重新协商PDU大小
            self._pdu_size = await self._get_pdu_size()
            logger.info(
                "[s7] device=%s code=RECONNECT_OK msg=Reconnected (rack=%d, slot=%d, pdu=%d)",
                ip, rack, slot, self._pdu_size,
            )
        except Exception as e:
            self._log_error(ip, "RECONNECT_FAILED", f"msg=Reconnect failed (rack={rack}, slot={slot}) - {e}")
