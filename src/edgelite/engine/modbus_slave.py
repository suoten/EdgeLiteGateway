"""内置Modbus Slave模拟器 - 提供Modbus TCP从站服务

Pro版特性：内置Modbus Slave模拟器，方便：
1. 开发调试：无需真实设备即可测试Modbus采集
2. 系统级联：其他Modbus主站可读取网关数据
3. 数据共享：将网关采集数据通过Modbus协议暴露给第三方系统
默认端口502，支持Coil/Discrete/Holding/Input四类寄存器。
"""

from __future__ import annotations

import asyncio
import logging
import struct
from typing import Any

try:
    from edgelite._cython import map_device_data_fast

    _HAS_CYTHON_MAPPER = True
except ImportError:
    _HAS_CYTHON_MAPPER = False
    map_device_data_fast = None

import contextlib

import pymodbus

_PYMODBUS_MAJOR = int(getattr(pymodbus, "__version__", "2.0.0").split(".")[0])

logger = logging.getLogger(__name__)


class ModbusSlaveServer:
    """内置Modbus Slave模拟器

    基于pymodbus的Server实现，提供：
    - Modbus TCP Slave服务
    - 四类寄存器空间：Coil(1x)、Discrete(2x)、Holding(3x)、Input(4x)
    - 数据映射：网关采集数据自动映射到Holding寄存器
    - 支持外部Modbus主站读取
    """

    def __init__(self):
        self._running = False
        self._server = None
        self._task: asyncio.Task | None = None
        self._context = None

    async def start(self, config: dict | None = None) -> None:
        """启动内置Modbus Slave

        Args:
            config: 配置参数
                host: 监听地址 (默认"0.0.0.0")
                port: 监听端口 (默认502)
                coils_size: Coil寄存器数量 (默认100)
                discrete_size: Discrete寄存器数量 (默认100)
                holding_size: Holding寄存器数量 (默认1000)
                input_size: Input寄存器数量 (默认1000)
        """
        try:
            from pymodbus.datastore import (
                ModbusSequentialDataBlock,
                ModbusServerContext,
            )
            from pymodbus.server import StartAsyncTcpServer

            try:
                from pymodbus.datastore import ModbusSlaveContext as _SlaveCtx
            except ImportError:
                from pymodbus.datastore import ModbusDeviceContext as _SlaveCtx
        except ImportError:
            logger.warning("pymodbus未安装，内置Modbus Slave不可用")
            return

        config = config or {}
        host = config.get("host", "0.0.0.0")
        port = int(config.get("port", 502))
        coils_size = int(config.get("coils_size", 100))
        discrete_size = int(config.get("discrete_size", 100))
        holding_size = int(config.get("holding_size", 1000))
        input_size = int(config.get("input_size", 1000))

        try:
            # 创建数据存储 (pymodbus 3.x 要求地址 >= 1)
            coils_block = ModbusSequentialDataBlock(1, [0] * coils_size)
            discrete_block = ModbusSequentialDataBlock(1, [0] * discrete_size)
            holding_block = ModbusSequentialDataBlock(1, [0] * holding_size)
            input_block = ModbusSequentialDataBlock(1, [0] * input_size)

            # pymodbus 3.x 使用 ModbusDeviceContext
            slave_context = _SlaveCtx(
                di=discrete_block,
                co=coils_block,
                hr=holding_block,
                ir=input_block,
            )
            # pymodbus 3.x 参数名变化: slaves -> devices
            if _PYMODBUS_MAJOR < 3:
                self._context = ModbusServerContext(slaves=slave_context, single=True)
            else:
                self._context = ModbusServerContext(devices=slave_context, single=True)

            # 启动Server
            self._task = asyncio.create_task(
                StartAsyncTcpServer(
                    context=self._context,
                    address=(host, port),
                ),
                name="modbus-slave",
            )
            self._running = True
            logger.info(
                "内置Modbus Slave启动: %s:%d (Holding=%d, Input=%d)",
                host,
                port,
                holding_size,
                input_size,
            )

        except Exception as e:
            logger.error("内置Modbus Slave启动失败: %s", e)
            self._task = None
            self._running = False

    async def stop(self) -> None:
        """停止内置Modbus Slave"""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._context = None
        logger.info("内置Modbus Slave已停止")

    async def set_holding_register(self, address: int, value: int) -> None:
        """设置Holding寄存器值

        Args:
            address: 寄存器地址 (0-based)
            value: 寄存器值 (16位无符号整数)
        """
        if not self._context:
            return
        try:
            self._context[0].setValues(3, address, [value])
        except Exception as e:
            logger.debug("Modbus Slave写入Holding失败: %s", e)

    async def set_input_register(self, address: int, value: int) -> None:
        """设置Input寄存器值"""
        if not self._context:
            return
        try:
            self._context[0].setValues(4, address, [value])
        except Exception as e:
            logger.debug("Modbus Slave写入Input失败: %s", e)

    async def set_coil(self, address: int, value: bool) -> None:
        """设置Coil值"""
        if not self._context:
            return
        try:
            self._context[0].setValues(1, address, [int(value)])
        except Exception as e:
            logger.debug("Modbus Slave写入Coil失败: %s", e)

    async def map_device_data(
        self, device_id: str, points: dict[str, Any], base_address: int = 0
    ) -> None:
        """将设备测点数据映射到Holding寄存器

        Args:
            device_id: 设备ID
            points: 测点数据 {point_name: value}
            base_address: 基地址偏移
        """
        if not self._context:
            return

        try:
            if _HAS_CYTHON_MAPPER:
                holding_size = 1000
                input_size = 1000
                coils_size = 100
                try:
                    hr = self._context[0].getValues(3, 0, holding_size)
                    ir = self._context[0].getValues(4, 0, input_size)
                    co = self._context[0].getValues(1, 0, coils_size)
                    holding_list = list(hr)
                    input_list = list(ir)
                    coils_list = list(co)
                except Exception:
                    holding_list = [0] * holding_size
                    input_list = [0] * input_size
                    coils_list = [0] * coils_size

                next_addr = map_device_data_fast(
                    points, holding_list, input_list, coils_list, base_address
                )

                try:  # FIXED: 原问题-3处setValues调用无异常保护
                    for i, v in enumerate(holding_list[base_address:next_addr]):
                        self._context[0].setValues(3, base_address + i, [v])
                    for i, v in enumerate(input_list):
                        self._context[0].setValues(4, i, [v])
                    for i, v in enumerate(coils_list):
                        self._context[0].setValues(1, i, [v])
                except Exception as e:
                    logger.error("Modbus Slave setValues失败: %s", e)
                    break
                return

            offset = base_address
            for _point_name, value in points.items():
                if isinstance(value, bool):
                    await self.set_coil(offset, value)
                    offset += 1
                elif isinstance(value, float):
                    raw = struct.pack(">f", value)
                    hi = struct.unpack(">H", raw[:2])[0]
                    lo = struct.unpack(">H", raw[2:])[0]
                    await self.set_holding_register(offset, hi)
                    await self.set_holding_register(offset + 1, lo)
                    offset += 2
                elif isinstance(value, int):
                    if -32768 <= value <= 65535:
                        await self.set_holding_register(offset, value & 0xFFFF)
                        offset += 1
                    else:
                        hi = (value >> 16) & 0xFFFF
                        lo = value & 0xFFFF
                        await self.set_holding_register(offset, hi)
                        await self.set_holding_register(offset + 1, lo)
                        offset += 2
                else:
                    offset += 1
        except Exception as e:
            logger.debug("Modbus Slave数据映射失败: %s", e)

    @property
    def is_running(self) -> bool:
        return self._running
