"""FANUC CNC驱动 - 基于FOCAS2 Ethernet协议直接实现

不依赖pyfanuc/fwlipy(已废弃4年)，直接通过asyncio socket实现FOCAS2 TCP二进制通信。
FOCAS2协议文档参考: FANUC FOCAS2 Ethernet API Specification

支持: 0i-D/0i-F/16i/18i/30i/31i/32i等系列
默认端口: 8193 (Ethernet)
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time
from typing import Any

from edgelite.drivers.base import DriverPlugin

logger = logging.getLogger(__name__)

_FOCAS_HEADER_FMT = "<HHHHHH"
_FOCAS_HEADER_SIZE = struct.calcsize(_FOCAS_HEADER_FMT)
_FOCAS_ID = 1

_FOCAS_FUNC_CNC_STATINFO = (3, 1)
_FOCAS_FUNC_CNC_RDPOSITION = (3, 21)
_FOCAS_FUNC_CNC_RDPROGNUM = (3, 2)
_FOCAS_FUNC_CNC_RDFEED = (3, 25)
_FOCAS_FUNC_CNC_RDSPINDLE = (3, 26)
_FOCAS_FUNC_CNC_ALARM2 = (3, 49)


class _FocasClient:
    """FOCAS2 Ethernet协议客户端 - 直接TCP通信实现"""

    def __init__(self, host: str, port: int = 8193, timeout: float = 5.0):
        self._host = host
        self._port = port
        self._timeout = timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self._host, self._port),
            timeout=self._timeout,
        )

    async def close(self) -> None:
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
            self._reader = None

    async def _send_request(self, reqh: int, reql: int, data: bytes = b"") -> bytes:
        if not self._writer or not self._reader:
            raise ConnectionError("FOCAS未连接")

        size = _FOCAS_HEADER_SIZE + len(data)
        header = struct.pack(_FOCAS_HEADER_FMT, _FOCAS_ID, size, reqh, reql, 0, 0)
        self._writer.write(header + data)
        await self._writer.drain()

        resp_header = await asyncio.wait_for(
            self._reader.readexactly(_FOCAS_HEADER_SIZE),
            timeout=self._timeout,
        )
        r_id, r_size, r_reqh, r_reql, r_resh, r_resl = struct.unpack(_FOCAS_HEADER_FMT, resp_header)
        result_code = (r_resh << 16) | r_resl
        if result_code != 0:
            raise RuntimeError(f"FOCAS错误: function=({r_reqh},{r_reql}) result={result_code}")

        body_size = r_size - _FOCAS_HEADER_SIZE
        if body_size > 0:
            body = await asyncio.wait_for(
                self._reader.readexactly(body_size),
                timeout=self._timeout,
            )
        else:
            body = b""
        return body

    async def read_status(self) -> dict[str, Any]:
        body = await self._send_request(*_FOCAS_FUNC_CNC_STATINFO)
        if len(body) < 10:
            raise RuntimeError("FOCAS响应数据不足: statinfo")
        run, motion, msto, emergency = struct.unpack_from("<HHHH", body, 2)
        return {
            "run": run,
            "motion": motion,
            "msto": msto,
            "emergency": emergency,
            "running": (run & 0x01) != 0,
        }

    async def read_position(self, pos_type: int = 0, max_axes: int = 8) -> dict[str, float]:
        data = struct.pack("<hh", pos_type, -max_axes)
        body = await self._send_request(*_FOCAS_FUNC_CNC_RDPOSITION, data)
        positions: dict[str, float] = {}
        axis_size = 11
        offset = 4
        for i in range(min(len(body) - offset, max_axes * axis_size) // axis_size):
            pos_data, dec, kind = struct.unpack_from("<lHB", body, offset + i * axis_size)
            name_byte = body[offset + i * axis_size + 7] if offset + i * axis_size + 7 < len(body) else 0
            name = chr(name_byte) if 65 <= name_byte <= 90 else f"A{i}"
            divisor = 10 ** (dec & 0x0F) if (dec & 0x0F) > 0 else 1
            positions[name] = pos_data / divisor
        return positions

    async def read_program_number(self) -> int:
        body = await self._send_request(*_FOCAS_FUNC_CNC_RDPROGNUM)
        if len(body) < 8:
            raise RuntimeError("FOCAS响应数据不足: prognum")
        main_prog, _, sub_prog = struct.unpack_from("<lhl", body, 2)
        return main_prog

    async def read_feedrate(self) -> float:
        body = await self._send_request(*_FOCAS_FUNC_CNC_RDFEED)
        if len(body) < 12:
            raise RuntimeError("FOCAS响应数据不足: feedrate")
        data, dec = struct.unpack_from("<lH", body, 4)
        divisor = 10 ** (dec & 0x0F) if (dec & 0x0F) > 0 else 1
        return data / divisor

    async def read_spindle_speed(self, max_spindles: int = 2) -> list[float]:
        data = struct.pack("<h", -max_spindles)
        body = await self._send_request(*_FOCAS_FUNC_CNC_RDSPINDLE, data)
        speeds = []
        spindle_size = 6
        offset = 2
        for i in range(min(len(body) - offset, max_spindles * spindle_size) // spindle_size):
            spd_data, dec = struct.unpack_from("<lH", body, offset + i * spindle_size)
            divisor = 10 ** (dec & 0x0F) if (dec & 0x0F) > 0 else 1
            speeds.append(spd_data / divisor)
        return speeds

    async def read_alarms(self, max_alarms: int = 10) -> list[dict[str, Any]]:
        data = struct.pack("<h", max_alarms)
        body = await self._send_request(*_FOCAS_FUNC_CNC_ALARM2, data)
        alarms = []
        alarm_size = 6
        offset = 2
        for i in range(min(len(body) - offset, max_alarms * alarm_size) // alarm_size):
            alm_no, alm_msg = struct.unpack_from("<h4s", body, offset + i * alarm_size)
            if alm_no != 0:
                alarms.append(
                    {
                        "number": alm_no,
                        "message": alm_msg.decode("ascii", errors="replace").strip(),
                    }
                )
        return alarms


class FanucCncDriver(DriverPlugin):
    """FANUC CNC驱动 - 直接FOCAS2 Ethernet协议实现（无需pyfanuc/fwlipy）

    配置参数:
        host: CNC控制器IP地址
        port: FOCAS端口 (默认8193)
        timeout: 连接超时秒 (默认5)
        max_axes: 最大轴数 (默认8)
    """

    plugin_name = "fanuc_cnc"
    plugin_version = "2.0.0"
    supported_protocols = ["fanuc", "focas"]
    config_schema = {
        "description": "FANUC CNC FOCAS2 protocol (native socket, no fwlipy required)",
        "fields": [
            {
                "name": "host",
                "type": "string",
                "label": "IP Address",
                "description": "CNC controller IP address",
                "default": "192.168.1.1",
                "required": True,
            },
            {
                "name": "port",
                "type": "integer",
                "label": "Port",
                "description": "FOCAS2 Ethernet port, default 8193",
                "default": 8193,
            },
            {
                "name": "timeout",
                "type": "integer",
                "label": "Timeout (s)",
                "description": "Connection and read timeout",
                "default": 5,
            },
            {
                "name": "max_axes",
                "type": "integer",
                "label": "Max Axes",
                "description": "Maximum number of axes to read",
                "default": 8,
            },
        ],
    }

    _MAX_RECONNECT_ATTEMPTS = 100
    _RECONNECT_BASE_DELAY = 1.0
    _RECONNECT_MAX_DELAY = 60.0

    def __init__(self):
        self._running = False
        self._client: _FocasClient | None = None
        self._config: dict = {}
        self._lock = asyncio.Lock()
        self._read_fail_tracker: dict[str, tuple[float, float]] = {}
        self._reconnect_count: int = 0
        self._reconnect_delay: float = self._RECONNECT_BASE_DELAY
        self._devices: dict[str, dict] = {}

    async def start(self, config: dict) -> None:
        self._config = config
        ip = config.get("host", "") or config.get("ip", "")
        port = int(config.get("port", 8193))
        timeout = float(config.get("timeout", 5))

        if not ip:
            raise ValueError("FANUC CNC驱动配置缺少host参数")

        if not (1 <= port <= 65535):
            raise ValueError(f"FANUC驱动port超出范围[1-65535]，当前: {port}")
        if timeout <= 0:
            raise ValueError(f"FANUC驱动timeout必须大于0，当前: {timeout}")

        self._client = _FocasClient(ip, port, timeout)
        try:
            await self._client.connect()
            self._running = True
            logger.info("FANUC CNC连接成功(FOCAS2直连): %s:%d", ip, port)
        except Exception as e:
            logger.error("FANUC CNC连接失败: %s - %s", ip, e)
            raise

    async def stop(self) -> None:
        self._running = False
        if self._client:
            await self._client.close()
            self._client = None
        logger.info("FANUC CNC驱动已停止")

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        if not self._running or not self._client:
            await self._try_reconnect(device_id)
            return {}

        result = {}
        async with self._lock:
            for point_name in points:
                try:
                    value = await self._read_point(point_name)
                    result[point_name] = value
                    self._read_fail_tracker.pop(point_name, None)
                except Exception as e:
                    self._log_throttled(device_id, point_name, e)
                    result[point_name] = None
        return result

    async def _read_point(self, name: str) -> Any:
        name_lower = name.lower()

        if name_lower == "cnc_status":
            return await self._client.read_status()
        elif name_lower == "cnc_position":
            return await self._client.read_position(max_axes=int(self._config.get("max_axes", 8)))
        elif name_lower == "cnc_feedrate":
            return await self._client.read_feedrate()
        elif name_lower == "cnc_spindle_speed":
            return await self._client.read_spindle_speed()
        elif name_lower == "cnc_program":
            return await self._client.read_program_number()
        elif name_lower == "cnc_alarm":
            return await self._client.read_alarms()
        elif name_lower.startswith("axis."):
            parts = name.split(".")
            if len(parts) >= 3:
                axis_name = parts[1].upper()
                positions = await self._client.read_position(max_axes=int(self._config.get("max_axes", 8)))
                return positions.get(axis_name)
        raise ValueError(f"未知FANUC测点: {name}")

    _LOG_INTERVAL = 60.0

    def _log_throttled(self, device_id: str, point_name: str, error: Exception) -> None:
        now = time.monotonic()
        first_time, last_log = self._read_fail_tracker.get(point_name, (now, 0.0))
        level = logging.WARNING if now - first_time < 5.0 else logging.DEBUG
        if now - last_log >= self._LOG_INTERVAL:
            logger.log(level, "FANUC读取失败: %s.%s - %s", device_id, point_name, error)
            self._read_fail_tracker[point_name] = (first_time, now)
        else:
            self._read_fail_tracker[point_name] = (first_time, last_log)

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        logger.warning("FANUC CNC当前版本不支持写入操作: %s", point)
        return False

    async def _try_reconnect(self, device_id: str) -> None:
        if not self._config:
            return
        self._reconnect_count += 1
        if self._reconnect_count > self._MAX_RECONNECT_ATTEMPTS:
            logger.error("FANUC重连放弃: %s (已重试%d次)", device_id, self._reconnect_count)
            self._running = False
            return
        delay = min(self._reconnect_delay, self._RECONNECT_MAX_DELAY)
        logger.warning("FANUC连接断开，%.1fs后重连 (第%d次): %s", delay, self._reconnect_count, device_id)
        await asyncio.sleep(delay)
        self._reconnect_delay *= 2
        ip = self._config.get("host", "") or self._config.get("ip", "")
        port = int(self._config.get("port", 8193))
        timeout = float(self._config.get("timeout", 5))
        if not ip:
            return
        if self._client:
            try:
                await self._client.close()
            except Exception:
                pass
        try:
            self._client = _FocasClient(ip, port, timeout)
            await self._client.connect()
            self._running = True
            self._reconnect_count = 0
            self._reconnect_delay = self._RECONNECT_BASE_DELAY
            logger.info("FANUC重连成功: %s:%d", ip, port)
        except Exception as e:
            logger.error("FANUC重连失败: %s - %s", ip, e)

    async def add_device(self, device_id: str, config: dict, points: list[dict] | None = None) -> None:
        """添加FANUC CNC设备，保存配置和测点映射"""
        if points is None:
            points = []
        self._devices[device_id] = {
            "config": config,
            "points": {p.get("name", p.get("address", "")): p for p in points if p.get("name") or p.get("address")},
        }
        logger.info("FANUC设备已添加: %s (%d测点)", device_id, len(points))

    async def discover_devices(self, config: dict) -> list[dict]:
        return []

    def remove_device(self, device_id: str) -> None:
        """Remove a device at runtime"""
        self._health_stats.pop(device_id, None)
        self._offline_since.pop(device_id, None)
        self._read_fail_tracker.pop(device_id, None)
        logger.info("FANUC device removed: %s", device_id)
