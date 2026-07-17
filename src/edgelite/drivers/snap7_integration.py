"""Profinet Snap7集成模块 - 提供与snap7库的Python绑定

Snap7是一个开源的西门子通信库，支持S7协议和Profinet通信。
本模块提供与snap7的Python绑定，支持:
- S7 PLC连接
- DB/Marker/Inputs/Outputs读写
- Profinet IO数据交换
- CPU状态读取

依赖:
    pip install python-snap7
    Linux: 可能需要编译snap7
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import logging
import platform
import struct
from dataclasses import dataclass
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

# Snap7库状态
SNAP7_AVAILABLE = False
_snap7_lib = None


# Snap7常量
class Snap7Error(Exception):
    """Snap7异常"""

    pass


class S7Area(Enum):
    """S7内存区域"""

    PE = 0x81  # Process Inputs
    PA = 0x82  # Process Outputs
    MK = 0x83  # Markers (Flags)
    DB = 0x84  # Data Blocks
    CT = 0x1C  # Counters
    TM = 0x1D  # Timers


class S7WordLen(Enum):
    """S7数据类型"""

    Bit = 0x01
    Byte = 0x02
    Word = 0x04
    DWord = 0x06
    Real = 0x08


# Snap7库名称
SNAP7_LIB_NAMES = {
    "Linux": ["libsnap7.so", "libsnap7.so.1"],
    "Windows": ["snap7.dll", "snap7-x64.dll", "snap7-x86.dll"],
    "Darwin": ["libsnap7.dylib", "libsnap7.1.dylib"],
}


def _try_load_snap7() -> bool:
    """尝试加载snap7库"""
    global SNAP7_AVAILABLE, _snap7_lib

    if SNAP7_AVAILABLE:
        return True

    system = platform.system()
    lib_names = SNAP7_LIB_NAMES.get(system, [])

    for lib_name in lib_names:
        try:
            path = ctypes.util.find_library(lib_name)
            if path:
                _snap7_lib = ctypes.CDLL(path)
                SNAP7_AVAILABLE = True
                logger.info("Snap7 library loaded: %s", path)
                return True
        except (OSError, AttributeError):
            continue

    logger.debug("Snap7 library not available, using simulation mode")
    return False


# 尝试加载snap7
_try_load_snap7()


@dataclass
class Snap7ConnectionInfo:
    """Snap7连接信息"""

    ip_address: str
    rack: int = 0
    slot: int = 1
    local_rack: int = 0
    local_slot: int = 0
    timeout: int = 5000


class Snap7Client:
    """Snap7客户端封装"""

    def __init__(self):
        self._client: ctypes.c_void_p | None = None
        self._connected: bool = False
        self._info: Snap7ConnectionInfo | None = None
        self._initialized: bool = False

        if SNAP7_AVAILABLE and _snap7_lib:
            self._init_snap7()

    def _init_snap7(self) -> bool:
        """初始化Snap7客户端"""
        if not SNAP7_AVAILABLE or _snap7_lib is None:
            return False

        try:
            _snap7_lib.Cli_Create.restype = ctypes.c_void_p
            _snap7_lib.Cli_Create.argtypes = []

            self._client = _snap7_lib.Cli_Create()
            if self._client:
                self._initialized = True
                logger.info("Snap7 client created")
                return True

        except Exception as e:
            logger.error("Snap7 initialization failed: %s", e)

        return False

    def connect(self, info: Snap7ConnectionInfo) -> bool:
        """连接到S7 PLC或Profinet设备

        Args:
            info: 连接信息

        Returns:
            连接是否成功
        """
        if not self._initialized:
            logger.warning("Snap7 not initialized, using simulation mode")
            self._info = info
            self._connected = True
            return True

        if _snap7_lib is None or self._client is None:
            return False

        try:
            _snap7_lib.Cli_ConnectTo.restype = ctypes.c_int
            _snap7_lib.Cli_ConnectTo.argtypes = [
                ctypes.c_void_p,
                ctypes.c_char_p,
                ctypes.c_uint16,
                ctypes.c_uint16,
            ]

            ip_bytes = info.ip_address.encode()
            result = _snap7_lib.Cli_ConnectTo(
                self._client,
                ip_bytes,
                info.rack,
                info.slot,
            )

            if result == 0:
                self._connected = True
                self._info = info
                logger.info("Snap7 connected to %s (rack=%d, slot=%d)", info.ip_address, info.rack, info.slot)
                return True
            else:
                logger.error("Snap7 connection failed: error=%d", result)
                return False

        except Exception as e:
            logger.error("Snap7 connection error: %s", e)
            return False

    def disconnect(self) -> None:
        """断开连接"""
        if self._initialized and _snap7_lib and self._client:
            try:
                _snap7_lib.Cli_Disconnect(self._client)
                logger.info("Snap7 disconnected")
            except Exception as e:
                logger.debug("Snap7 disconnect error: %s", e)

        self._connected = False

    def destroy(self) -> None:
        """销毁客户端"""
        if self._initialized and _snap7_lib and self._client:
            try:
                _snap7_lib.Cli_Destroy(ctypes.byref(ctypes.c_void_p(self._client)))
            except Exception as e:
                logger.debug("[snap7] Cli_Destroy failed: %s", e)
        self._client = None
        self._initialized = False

    def read_area(self, area: S7Area, db_number: int, start: int, size: int) -> bytes | None:
        """读取S7内存区域

        Args:
            area: 内存区域 (PE/PA/MK/DB/CT/TM)
            db_number: DB编号 (对于DB区域)
            start: 起始字节
            size: 读取字节数

        Returns:
            读取的原始数据
        """
        if not self._connected:
            logger.warning("Snap7 not connected")
            return None

        if not self._initialized or _snap7_lib is None or self._client is None:
            # 模拟模式
            return bytes(size)

        try:
            buffer = ctypes.create_string_buffer(size)
            ctypes.c_int32(size)

            result = _snap7_lib.Cli_ReadArea(
                self._client,
                area.value,
                db_number,
                start,
                size,
                0,  # wordlen (0=byte)
                buffer,
            )

            if result == 0:
                return buffer.raw
            else:
                logger.error("Snap7 read_area failed: error=%d", result)
                return None

        except Exception as e:
            logger.error("Snap7 read_area error: %s", e)
            return None

    def write_area(self, area: S7Area, db_number: int, start: int, data: bytes) -> bool:
        """写入S7内存区域

        Args:
            area: 内存区域
            db_number: DB编号
            start: 起始字节
            data: 要写入的数据

        Returns:
            是否成功
        """
        if not self._connected:
            logger.warning("Snap7 not connected")
            return False

        if not self._initialized or _snap7_lib is None or self._client is None:
            # 模拟模式
            return True

        try:
            buffer = ctypes.create_string_buffer(data)
            size = len(data)

            result = _snap7_lib.Cli_WriteArea(
                self._client,
                area.value,
                db_number,
                start,
                size,
                0,  # wordlen
                buffer,
            )

            if result == 0:
                logger.debug(
                    "Snap7 write_area success: area=%s db=%d start=%d size=%d", area.name, db_number, start, size
                )
                return True
            else:
                logger.error("Snap7 write_area failed: error=%d", result)
                return False

        except Exception as e:
            logger.error("Snap7 write_area error: %s", e)
            return False

    def get_cpu_state(self) -> str | None:
        """获取CPU状态"""
        if not self._initialized or _snap7_lib is None or self._client is None:
            return "RUN"  # 模拟模式

        try:
            state = ctypes.c_int(0)
            result = _snap7_lib.Cli_GetCpuState(self._client, ctypes.byref(state))

            if result == 0:
                states = ["Unknown", "Running", "Stopped", "Unknown"]
                return states[state.value] if state.value < len(states) else "Unknown"
            return None

        except Exception as e:
            logger.error("Snap7 get_cpu_state error: %s", e)
            return None

    def read_db_float32(self, db_number: int, start: int) -> float | None:
        """读取DB中的float32值"""
        data = self.read_area(S7Area.DB, db_number, start, 4)
        if data and len(data) >= 4:
            return struct.unpack(">f", data[:4])[0]
        return None

    def read_db_int16(self, db_number: int, start: int) -> int | None:
        """读取DB中的int16值"""
        data = self.read_area(S7Area.DB, db_number, start, 2)
        if data and len(data) >= 2:
            return struct.unpack(">h", data[:2])[0]
        return None

    def read_db_uint16(self, db_number: int, start: int) -> int | None:
        """读取DB中的uint16值"""
        data = self.read_area(S7Area.DB, db_number, start, 2)
        if data and len(data) >= 2:
            return struct.unpack(">H", data[:2])[0]
        return None

    def write_db_float32(self, db_number: int, start: int, value: float) -> bool:
        """写入DB中的float32值"""
        data = struct.pack(">f", value)
        return self.write_area(S7Area.DB, db_number, start, data)

    def write_db_int16(self, db_number: int, start: int, value: int) -> bool:
        """写入DB中的int16值"""
        data = struct.pack(">h", value)
        return self.write_area(S7Area.DB, db_number, start, data)

    @property
    def is_connected(self) -> bool:
        """是否已连接"""
        return self._connected

    @property
    def is_available(self) -> bool:
        """Snap7库是否可用"""
        return SNAP7_AVAILABLE


class ProfinetSnap7Bridge:
    """Profinet-Snap7桥接器

    对于不支持直接Profinet IO的设备，可以通过Snap7连接
    同一网络中的S7 PLC来实现数据交换。
    """

    def __init__(self):
        self._s7_client = Snap7Client()
        self._mapping: dict[str, tuple[int, int, int]] = {}  # pn_addr -> (db, start, size)

    def connect_to_plc(self, ip: str, rack: int = 0, slot: int = 1) -> bool:
        """连接到S7 PLC"""
        info = Snap7ConnectionInfo(ip, rack, slot)
        return self._s7_client.connect(info)

    def map_pn_to_db(
        self,
        pn_slot: int,
        pn_subslot: int,
        pn_index: int,
        db_number: int,
        db_start: int,
        size: int = 2,
    ) -> None:
        """建立Profinet地址到DB的映射

        Args:
            pn_slot: Profinet slot
            pn_subslot: Profinet subslot
            pn_index: Profinet IO index
            db_number: S7 DB number
            db_start: DB start byte
            size: 数据大小 (bytes)
        """
        key = f"{pn_slot}:{pn_subslot}:{pn_index}"
        self._mapping[key] = (db_number, db_start, size)
        logger.info("Mapped Profinet %s -> DB%d.%d", key, db_number, db_start)

    def read_io_data(self, slot: int, subslot: int, index: int, size: int) -> bytes | None:
        """通过映射读取IO数据"""
        key = f"{slot}:{subslot}:{index}"
        if key in self._mapping:
            db, start, _ = self._mapping[key]
            return self._s7_client.read_area(S7Area.DB, db, start, size)
        return None

    def write_io_data(self, slot: int, subslot: int, index: int, data: bytes) -> bool:
        """通过映射写入IO数据"""
        key = f"{slot}:{subslot}:{index}"
        if key in self._mapping:
            db, start, _ = self._mapping[key]
            return self._s7_client.write_area(S7Area.DB, db, start, data)
        return False

    def disconnect(self) -> None:
        """断开连接"""
        self._s7_client.disconnect()

    def destroy(self) -> None:
        """销毁桥接器"""
        self._s7_client.destroy()

    @property
    def is_connected(self) -> bool:
        return self._s7_client.is_connected
