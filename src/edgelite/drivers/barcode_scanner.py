"""扫码枪驱动 - 支持USB/串口扫码枪设备接入

扫码枪通常通过USB虚拟串口或键盘模拟方式工作。
本驱动支持串口模式扫码枪，自动解析条码数据帧。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from edgelite.drivers.base import DriverPlugin

logger = logging.getLogger(__name__)


class BarcodeScannerDriver(DriverPlugin):
    """扫码枪驱动

    配置参数:
        port: 串口设备路径 (如 /dev/ttyUSB0, COM3)
        baudrate: 波特率 (默认9600)
        prefix: 条码前缀 (可选，用于过滤)
        suffix: 条码后缀 (默认\\r，即回车)
        barcode_types: 支持的条码类型过滤 (可选，如 ["CODE128", "QR"])
    """

    plugin_name = "barcode_scanner"
    plugin_version = "1.0.0"
    supported_protocols = ["barcode_scanner"]

    def __init__(self):
        self._running = False
        self._serial = None
        self._config: dict = {}
        self._read_task: asyncio.Task | None = None
        self._data_callback = None
        self._buffer: str = ""
        self._latest_barcodes: dict[str, str] = {}

    async def start(self, config: dict) -> None:
        try:
            import serial
        except ImportError:
            raise ImportError("pyserial未安装，请执行: pip install pyserial")

        self._config = config
        port = config.get("port", "COM1")
        baudrate = int(config.get("baudrate", 9600))

        try:
            self._serial = serial.Serial(
                port=port,
                baudrate=baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.1,
            )
            self._running = True
            self._read_task = asyncio.create_task(self._read_loop(), name="barcode-read")
            logger.info("扫码枪驱动启动成功 (port=%s)", port)
        except Exception as e:
            logger.error("扫码枪驱动启动失败: %s", e)
            raise

    async def stop(self) -> None:
        self._running = False
        if self._read_task and not self._read_task.done():
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass
        if self._serial and self._serial.is_open:
            try:
                self._serial.close()
            except Exception as e:
                logger.debug("串口关闭失败: %s", e)
        self._serial = None
        logger.info("扫码枪驱动已停止")

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        result = {}
        for point in points:
            result[point] = self._latest_barcodes.get(point)
        return result

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        return False

    def on_data(self, callback) -> None:
        self._data_callback = callback

    async def _read_loop(self) -> None:
        suffix = self._config.get("suffix", "\r")
        prefix = self._config.get("prefix", "")

        while self._running:
            try:
                if self._serial and self._serial.in_waiting > 0:
                    data = await asyncio.to_thread(self._serial.read, self._serial.in_waiting)
                    text = data.decode("utf-8", errors="replace")
                    self._buffer += text

                    while suffix in self._buffer:
                        line, self._buffer = self._buffer.split(suffix, 1)
                        line = line.strip()

                        if prefix and not line.startswith(prefix):
                            continue

                        barcode = line[len(prefix):] if prefix else line
                        if barcode:
                            self._latest_barcodes["barcode"] = barcode
                            self._latest_barcodes["last_scan"] = barcode
                            logger.info("扫码枪读取: %s", barcode)

                            if self._data_callback:
                                await self._data_callback({
                                    "point": "barcode",
                                    "value": barcode,
                                    "raw": line,
                                })
                await asyncio.sleep(0.02)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("扫码枪读取异常: %s", e)
                await asyncio.sleep(0.5)

    async def discover_devices(self, config: dict) -> list[dict]:
        try:
            import serial.tools.list_ports
        except ImportError:
            return []

        ports = serial.tools.list_ports.comports()
        result = []
        for p in ports:
            if any(kw in (p.description or "").lower() or kw in (p.manufacturer or "").lower()
                   for kw in ["scanner", "barcode", "symbol", "zebra", "honeywell", "datalogic"]):
                result.append({
                    "device_id": p.device,
                    "name": p.description,
                    "ip": p.device,
                    "protocol": "barcode_scanner",
                })
        return result
