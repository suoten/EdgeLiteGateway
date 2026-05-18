"""串口TCP透传桥接模块 - 串口数据双向转发到TCP客户端"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass

from edgelite.constants import _EVENT_BUS_MAX_QUEUE, _SERIAL_READ_TIMEOUT

logger = logging.getLogger(__name__)

DEFAULT_TCP_PORT = 9600
DEFAULT_BUFFER_SIZE = 4096
RECONNECT_DELAY = 2.0
MAX_RECONNECT_DELAY = 30.0


@dataclass
class BridgeStats:
    """桥接统计信息"""

    serial_rx_bytes: int = 0
    serial_tx_bytes: int = 0
    tcp_rx_bytes: int = 0
    tcp_tx_bytes: int = 0
    client_count: int = 0
    total_connections: int = 0
    start_time: float = 0.0
    running: bool = False


class SerialTcpBridge:
    """串口-TCP透传桥接

    配置参数:
        serial_port: 串口设备路径 (如 /dev/ttyUSB0, COM3)
        baudrate: 波特率 (默认9600)
        bytesize: 数据位 5/6/7/8 (默认8)
        parity: 校验位 N/E/O (默认N)
        stopbits: 停止位 1/2 (默认1)
        tcp_host: TCP监听地址 (默认0.0.0.0)
        tcp_port: TCP监听端口 (默认9600)
        allowed_ips: IP白名单列表 (空=不限制)
        buffer_size: 缓冲区大小 (默认4096)
    """

    def __init__(self):
        self._running = False
        self._serial = None
        self._config: dict = {}
        self._tcp_server: asyncio.AbstractServer | None = None
        self._clients: dict[asyncio.StreamReader, asyncio.StreamWriter] = {}
        self._stats = BridgeStats()
        self._serial_queue: asyncio.Queue = asyncio.Queue(maxsize=_EVENT_BUS_MAX_QUEUE)  # FIXED: 原问题-maxsize=10000魔法数字
        self._relay_task: asyncio.Task | None = None
        self._serial_read_task: asyncio.Task | None = None

    async def start(self, config: dict) -> None:
        """启动串口TCP透传桥接"""
        try:
            import serial
        except ImportError:
            raise ImportError("pyserial未安装，请执行: pip install pyserial") from None

        self._config = config
        self._stats = BridgeStats(start_time=time.time(), running=True)

        serial_port = str(config.get("serial_port", "COM1"))
        try:  # FIXED: 原问题-int()转换无ValueError保护
            baudrate = int(config.get("baudrate", 9600))
        except (ValueError, TypeError):
            baudrate = 9600
        try:  # FIXED: 原问题-int()转换无ValueError保护
            bytesize = int(config.get("bytesize", 8))
        except (ValueError, TypeError):
            bytesize = 8
        parity = str(config.get("parity", "N"))
        try:  # FIXED: 原问题-float()转换无ValueError保护
            stopbits = float(config.get("stopbits", 1))
        except (ValueError, TypeError):
            stopbits = 1.0

        parity_map = {"N": serial.PARITY_NONE, "E": serial.PARITY_EVEN, "O": serial.PARITY_ODD}
        bytesize_map = {
            5: serial.FIVEBITS,
            6: serial.SIXBITS,
            7: serial.SEVENBITS,
            8: serial.EIGHTBITS,
        }
        stopbits_map = {1: serial.STOPBITS_ONE, 2: serial.STOPBITS_TWO}

        try:
            self._serial = serial.Serial(
                port=serial_port,
                baudrate=baudrate,
                bytesize=bytesize_map.get(bytesize, serial.EIGHTBITS),
                parity=parity_map.get(parity, serial.PARITY_NONE),
                stopbits=stopbits_map.get(int(stopbits), serial.STOPBITS_ONE),
                timeout=_SERIAL_READ_TIMEOUT,  # FIXED: 原问题-timeout=0.1魔法数字
            )
            logger.info("串口打开成功: %s @ %d", serial_port, baudrate)
        except serial.SerialException as e:
            logger.error("串口打开失败: %s - %s", serial_port, e)
            raise RuntimeError(
                f"串口 '{serial_port}' 打开失败: {e}。"
                f"请检查: 1)串口设备是否存在 2)是否有访问权限 3)是否被其他程序占用"
            ) from None
        except Exception as e:
            logger.error("串口打开异常: %s - %s", serial_port, e)
            raise RuntimeError(f"串口 '{serial_port}' 初始化异常: {e}") from e

        tcp_host = config.get("tcp_host", "0.0.0.0")
        tcp_port = int(config.get("tcp_port", DEFAULT_TCP_PORT))

        try:
            self._tcp_server = await asyncio.start_server(
                self._handle_client,
                tcp_host,
                tcp_port,
            )
            logger.info("TCP透传监听启动: %s:%d", tcp_host, tcp_port)
        except OSError as e:
            if self._serial and self._serial.is_open:
                self._serial.close()
            logger.error("TCP监听启动失败: %s:%d - %s", tcp_host, tcp_port, e)
            raise RuntimeError(f"TCP监听端口 {tcp_port} 启动失败: {e}。请检查端口是否被占用") from e
        except Exception as e:
            if self._serial and self._serial.is_open:
                self._serial.close()
            logger.error("TCP监听启动异常: %s", e)
            raise RuntimeError(f"TCP服务器启动异常: {e}") from e

        self._running = True
        self._serial_read_task = asyncio.create_task(
            self._serial_read_loop(), name="serial-bridge-read"
        )
        self._relay_task = asyncio.create_task(
            self._serial_to_tcp_relay(), name="serial-bridge-relay"
        )

    async def stop(self) -> None:
        """停止桥接"""
        self._running = False
        self._stats.running = False

        for task in (self._serial_read_task, self._relay_task):
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._serial_read_task = None
        self._relay_task = None

        for _reader, writer in list(self._clients.items()):
            try:
                writer.close()
                await writer.wait_closed()
            except Exception as e:
                logger.debug("关闭TCP客户端失败: %s", e)
        self._clients.clear()

        if self._tcp_server:
            self._tcp_server.close()
            await self._tcp_server.wait_closed()
            self._tcp_server = None

        if self._serial and self._serial.is_open:
            try:
                self._serial.close()
            except Exception as e:
                logger.debug("关闭串口失败: %s", e)
        self._serial = None
        logger.info("串口TCP透传桥接已停止")

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """处理TCP客户端连接"""
        peer = writer.get_extra_info("peername")
        client_ip = peer[0] if peer else "unknown"

        allowed_ips = self._config.get("allowed_ips", [])
        if allowed_ips and client_ip not in allowed_ips:
            logger.warning("IP白名单拒绝连接: %s", client_ip)
            writer.close()
            await writer.wait_closed()
            return

        logger.info("TCP客户端连接: %s", peer)
        self._clients[reader] = writer
        self._stats.client_count = len(self._clients)
        self._stats.total_connections += 1

        try:
            await self._tcp_to_serial_loop(reader)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning("TCP客户端处理异常: %s - %s", peer, e)
        finally:
            self._clients.pop(reader, None)
            self._stats.client_count = len(self._clients)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception as e:
                logger.debug("关闭客户端连接失败: %s", e)
            logger.info("TCP客户端断开: %s", peer)

    async def _tcp_to_serial_loop(self, reader: asyncio.StreamReader) -> None:
        """TCP→串口数据转发"""
        buffer_size = int(self._config.get("buffer_size", DEFAULT_BUFFER_SIZE))
        while self._running:
            try:
                data = await reader.read(buffer_size)
                if not data:
                    break
                self._stats.tcp_rx_bytes += len(data)
                if self._serial:
                    await asyncio.to_thread(self._serial.write, data)
                self._stats.serial_tx_bytes += len(data)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("TCP→串口转发异常: %s", e)
                break

    async def _serial_read_loop(self) -> None:
        """串口读取循环，将数据放入队列"""
        buffer_size = int(self._config.get("buffer_size", DEFAULT_BUFFER_SIZE))
        while self._running:
            try:
                if self._serial and self._serial.in_waiting > 0:
                    data = await asyncio.to_thread(
                        self._serial.read,
                        min(self._serial.in_waiting, buffer_size),
                    )
                    if data:
                        self._stats.serial_rx_bytes += len(data)
                        try:
                            self._serial_queue.put_nowait(data)
                        except asyncio.QueueFull:
                            with contextlib.suppress(asyncio.QueueEmpty):
                                self._serial_queue.get_nowait()
                            self._serial_queue.put_nowait(data)
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("串口读取异常: %s", e)
                await asyncio.sleep(0.5)

    async def _serial_to_tcp_relay(self) -> None:
        """串口→TCP数据转发（从队列取数据广播给所有客户端）"""
        while self._running:
            try:
                data = await asyncio.wait_for(self._serial_queue.get(), timeout=1.0)
                dead_clients = []
                for reader, writer in self._clients.items():
                    try:
                        writer.write(data)
                        await writer.drain()
                        self._stats.tcp_tx_bytes += len(data)
                    except Exception:
                        dead_clients.append(reader)

                for reader in dead_clients:
                    writer = self._clients.pop(reader, None)
                    if writer:
                        try:
                            writer.close()
                            await writer.wait_closed()
                        except Exception as e:
                            logger.debug("关闭写入端失败: %s", e)
                    self._stats.client_count = len(self._clients)
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("串口→TCP转发异常: %s", e)
                await asyncio.sleep(0.1)

    def get_status(self) -> BridgeStats:
        """获取桥接统计信息"""
        self._stats.client_count = len(self._clients)
        self._stats.running = self._running
        return self._stats
