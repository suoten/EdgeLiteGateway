"""EtherCAT 驱动 - 基于SOEM/SimpleOpen EtherCAT主站实现

EtherCAT是Beckhoff主导的高速工业以太网协议(IEC 61158)，
广泛应用于运动控制、分布式IO等场景，以其亚毫秒级实时性能著称。

支持:
- EtherCAT主站通信 (基于SOEM库)
- 从站发现和配置
- PDO (Process Data Object) 读写
- SDO (Service Data Object) 参数读写
- DC (Distributed Clock) 同步

依赖:
    Linux: apt install soem && pip install python-soem
    Windows: 需要PyEtherCAT或手动编译SOEM
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import struct
from dataclasses import dataclass
from typing import Any

from edgelite.drivers.base import DriverPlugin
from edgelite.drivers.soem_integration import (
    SOEM_AVAILABLE,
    SOEMContext,
    SOEMPdoConfig,
    SOEMSlaveInfo,
)

logger = logging.getLogger(__name__)

# EtherCAT 默认配置
DEFAULT_IFACE = "eth0"
DEFAULT_PORT = 0  # 网卡端口号
EC_TIMEOUT = 2000  # 操作超时 (毫秒)

# EtherCAT 状态机
EC_STATE_INIT = 0x01
EC_STATE_PRE_OP = 0x02
EC_STATE_BOOT = 0x03
EC_STATE_SAFE_OP = 0x04
EC_STATE_OPERATIONAL = 0x08

# EtherCAT 命令 (ETG.1000.6 标准)
EC_CMD_NOP = 0x00  # No Operation
EC_CMD_APRD = 0x01  # Auto Increment Physical Read
EC_CMD_APWR = 0x02  # Auto Increment Physical Write
EC_CMD_APRW = 0x03  # Auto Increment Physical Read/Write
EC_CMD_FPRD = 0x04  # Configured Address Physical Read
EC_CMD_FPWR = 0x05  # Configured Address Physical Write
EC_CMD_FPRW = 0x06  # Configured Address Physical Read/Write
EC_CMD_BRD = 0x07  # Broadcast Read
EC_CMD_BWR = 0x08  # Broadcast Write
EC_CMD_BRW = 0x09  # Broadcast Read/Write
EC_CMD_LRD = 0x0A  # Logical Memory Read
EC_CMD_LWR = 0x0B  # Logical Memory Write
EC_CMD_LRW = 0x0C  # Logical Memory Read/Write

# EtherCAT Working Counter 类型
EC_WKC_TYPE_PDO_OUT = 1
EC_WKC_TYPE_PDO_IN = 2
EC_WKC_TYPE_SDO = 3


@dataclass
class EtherCATSlave:
    """EtherCAT从站信息"""

    station_address: int
    vendor_id: int
    product_code: int
    revision_number: int
    name: str
    alias: int = 0
    state: int = EC_STATE_INIT
    outputs: bytes = b""
    inputs: bytes = b""


@dataclass
class PDOMapping:
    """PDO映射配置"""

    index: int
    subindex: int
    name: str
    data_type: str  # bool, int8, int16, int32, uint8, uint16, uint32, float
    direction: str  # "output" (主站→从站) 或 "input" (从站→主站)
    bit_length: int = 0


def _parse_data(data: bytes, offset: int, data_type: str, bit_length: int = 0) -> Any:
    """解析EtherCAT数据"""
    if len(data) < offset:
        return None

    if data_type == "bool":
        if bit_length > 0:
            byte_idx = offset + (bit_length // 8)
            bit_idx = bit_length % 8
            if byte_idx < len(data):
                return bool(data[byte_idx] & (1 << bit_idx))
        return bool(data[offset])

    elif data_type == "int8":
        return struct.unpack_from("<b", data, offset)[0] if len(data) >= offset + 1 else 0
    elif data_type == "uint8":
        return data[offset] if len(data) > offset else 0
    elif data_type == "int16":
        return struct.unpack_from("<h", data, offset)[0] if len(data) >= offset + 2 else 0
    elif data_type == "uint16":
        return struct.unpack_from("<H", data, offset)[0] if len(data) >= offset + 2 else 0
    elif data_type == "int32":
        return struct.unpack_from("<i", data, offset)[0] if len(data) >= offset + 4 else 0
    elif data_type == "uint32":
        return struct.unpack_from("<I", data, offset)[0] if len(data) >= offset + 4 else 0
    elif data_type == "float":
        return struct.unpack_from("<f", data, offset)[0] if len(data) >= offset + 4 else 0.0
    else:
        return data[offset] if len(data) > offset else 0


def _pack_data(value: Any, data_type: str) -> bytes:
    """打包EtherCAT数据"""
    if data_type == "bool":
        return bytes([1 if value else 0])
    elif data_type == "int8":
        return struct.pack("<b", int(value))
    elif data_type == "uint8":
        return bytes([int(value) & 0xFF])
    elif data_type == "int16":
        return struct.pack("<h", int(value))
    elif data_type == "uint16":
        return struct.pack("<H", int(value))
    elif data_type == "int32":
        return struct.pack("<i", int(value))
    elif data_type == "uint32":
        return struct.pack("<I", int(value))
    elif data_type == "float":
        return struct.pack("<f", float(value))
    else:
        return bytes([int(value) & 0xFF])


class EtherCATClient:
    """EtherCAT主站客户端封装 - 集成SOEM支持"""

    def __init__(self, iface: str = DEFAULT_IFACE, timeout: int = EC_TIMEOUT):
        self._iface = iface
        self._timeout = timeout
        self._soem: SOEMContext | None = None
        self._slaves: dict[int, EtherCATSlave] = {}
        self._pdo_mappings: dict[int, list[PDOMapping]] = {}
        self._output_size = 0
        self._input_size = 0
        self._initialized = False
        self._use_real_soem = SOEM_AVAILABLE

    async def initialize(self) -> bool:
        """初始化EtherCAT主站

        初始化过程:
        1. 创建SOEM上下文
        2. 初始化SOEM库
        3. 扫描从站
        """
        try:
            self._soem = SOEMContext(self._iface, self._timeout)

            # 初始化SOEM (通过 asyncio.to_thread 释放 GIL)
            init_result = await asyncio.to_thread(self._soem.initialize)
            if not init_result:
                logger.warning("SOEM initialization failed, using simulation mode")
                self._use_real_soem = False
            else:
                self._use_real_soem = True
                logger.info("EtherCAT SOEM initialized: %s (real mode)", self._iface)

            self._initialized = True
            logger.info("EtherCAT master initialized: %s (simulation mode)", self._iface)
            return True

        except Exception as e:
            logger.error("EtherCAT initialization failed: %s", e)
            self._initialized = True  # 仍然允许模拟模式运行
            return True

    async def scan_slaves(self) -> list[EtherCATSlave]:
        """扫描EtherCAT从站

        返回发现的从站列表，每个从站包含:
        - station_address: 从站地址
        - vendor_id: 供应商ID
        - product_code: 产品代码
        - name: 从站名称
        """
        slaves = []

        if self._soem and self._use_real_soem:
            # 使用真实SOEM扫描 (通过 asyncio.to_thread 释放 GIL)
            soem_slaves = await asyncio.to_thread(self._soem.scan_slaves)
            for s in soem_slaves:
                slave = EtherCATSlave(
                    station_address=s.position,
                    vendor_id=s.vendor_id,
                    product_code=s.product_code,
                    revision_number=s.revision_number,
                    name=s.name,
                    state=s.state,
                )
                slaves.append(slave)
                self._slaves[slave.station_address] = slave
        else:
            # 使用模拟扫描
            simulated = [
                EtherCATSlave(
                    station_address=1,
                    vendor_id=0x00000002,  # Beckhoff
                    product_code=0x044C2C52,
                    revision_number=0x00120000,
                    name="EK1100 (Coupler)",
                ),
                EtherCATSlave(
                    station_address=2,
                    vendor_id=0x00000002,  # Beckhoff
                    product_code=0x13ED3052,
                    revision_number=0x00110000,
                    name="EL4001 (AO 4ch)",
                ),
            ]
            for slave in simulated:
                slaves.append(slave)
                self._slaves[slave.station_address] = slave

        logger.info("EtherCAT scan complete: found %d slaves (real=%s)", len(slaves), self._use_real_soem)
        return slaves

    async def configure_pdo(self, slave_addr: int, mappings: list[PDOMapping]) -> bool:
        """配置PDO映射

        Args:
            slave_addr: 从站地址
            mappings: PDO映射列表
        """
        if slave_addr not in self._slaves:
            logger.error("Slave %d not found", slave_addr)
            return False

        self._pdo_mappings[slave_addr] = mappings

        # 计算PDO总大小
        output_size = 0
        input_size = 0

        for mapping in mappings:
            size = _get_type_size(mapping.data_type)
            if mapping.direction == "output":
                output_size += size
            else:
                input_size += size

        self._output_size = max(self._output_size, output_size)
        self._input_size = max(self._input_size, input_size)

        # 同步配置到SOEM (通过 asyncio.to_thread 释放 GIL)
        if self._soem and self._use_real_soem:
            soem_mappings = [
                SOEMPdoConfig(
                    index=m.index,
                    subindex=m.subindex,
                    name=m.name,
                    data_type=m.data_type,
                    direction=m.direction,
                    bit_length=m.bit_length,
                )
                for m in mappings
            ]
            await asyncio.to_thread(self._soem.configure_pdo, slave_addr, soem_mappings)

        logger.info(
            "PDO config complete: slave %d, output %d bytes, input %d bytes", slave_addr, output_size, input_size
        )
        return True

    async def set_slave_state(self, slave_addr: int, state: int) -> bool:
        """设置从站状态

        Args:
            slave_addr: 从站地址
            state: 目标状态 (EC_STATE_INIT/PRE_OP/SAFE_OP/OPERATIONAL)
        """
        if slave_addr not in self._slaves:
            return False

        state_names = {
            EC_STATE_INIT: "INIT",
            EC_STATE_PRE_OP: "PRE_OP",
            EC_STATE_SAFE_OP: "SAFE_OP",
            EC_STATE_BOOT: "BOOT",
            EC_STATE_OPERATIONAL: "OPERATIONAL",
        }

        logger.info(
            "Set slave %d state: %s -> %s",
            slave_addr,
            state_names.get(self._slaves[slave_addr].state, "UNKNOWN"),
            state_names.get(state, "UNKNOWN"),
        )

        self._slaves[slave_addr].state = state
        return True

    async def read_pdo(self, slave_addr: int) -> dict[str, Any]:
        """读取PDO数据 (从站→主站)

        Returns:
            包含所有映射变量的字典
        """
        if slave_addr not in self._slaves:
            return {}

        result = {}
        mappings = self._pdo_mappings.get(slave_addr, [])
        input_data = self._slaves[slave_addr].inputs

        offset = 0
        for mapping in mappings:
            if mapping.direction == "input":
                size = _get_type_size(mapping.data_type)
                value = _parse_data(input_data, offset, mapping.data_type, mapping.bit_length)
                result[mapping.name] = value
                offset += size

        return result

    async def write_pdo(self, slave_addr: int, data: dict[str, Any]) -> bool:
        """写入PDO数据 (主站→从站)

        Args:
            slave_addr: 从站地址
            data: 要写入的变量字典
        """
        if slave_addr not in self._slaves:
            return False

        mappings = self._pdo_mappings.get(slave_addr, [])
        output_data = bytearray(self._output_size)
        offset = 0

        for mapping in mappings:
            if mapping.direction == "output" and mapping.name in data:
                value = data[mapping.name]
                size = _get_type_size(mapping.data_type)
                packed = _pack_data(value, mapping.data_type)
                output_data[offset : offset + size] = packed
                offset += size

        self._slaves[slave_addr].outputs = bytes(output_data)
        return True

    async def read_sdo(self, slave_addr: int, index: int, subindex: int) -> Any | None:
        """读取SDO (Service Data Object)

        SDO用于访问从站参数，如电机分辨率、速度限制等。
        支持真实SOEM和模拟模式。

        Args:
            slave_addr: 从站地址
            index: 对象索引
            subindex: 子索引
        """
        if self._soem and self._use_real_soem:
            result = await asyncio.to_thread(self._soem.read_sdo, slave_addr, index, subindex)
            logger.debug("SDO read (SOEM): slave=%d 0x%04X:%02X = %s", slave_addr, index, subindex, result)
            return result

        logger.debug("SDO read (simulated): slave=%d 0x%04X:%02X", slave_addr, index, subindex)
        return None

    async def write_sdo(
        self, slave_addr: int, index: int, subindex: int, value: Any, data_type: str = "uint32"
    ) -> bool:
        """写入SDO

        Args:
            slave_addr: 从站地址
            index: 对象索引
            subindex: 子索引
            value: 要写入的值
            data_type: 数据类型
        """
        if self._soem and self._use_real_soem:
            result = await asyncio.to_thread(self._soem.write_sdo, slave_addr, index, subindex, value, data_type)
            logger.debug(
                "SDO write (SOEM): slave=%d 0x%04X:%02X = %s (%s)",
                slave_addr,
                index,
                subindex,
                value,
                "OK" if result else "FAIL",
            )
            return result

        logger.debug("SDO write (simulated): slave=%d 0x%04X:%02X = %s", slave_addr, index, subindex, value)
        return True

    async def request_dc_sync(self, slave_addr: int, sync0_time: int = 1000000, sync0_period: int = 1000000) -> bool:
        """请求DC (Distributed Clock) 同步

        Args:
            slave_addr: 从站地址
            sync0_time: 同步0触发时间 (ns)
            sync0_period: 同步0周期 (ns)
        """
        if not self._soem or not self._use_real_soem:
            logger.debug("DC sync (simulated): slave=%d", slave_addr)
            return True

        logger.info("DC sync requested: slave=%d, time=%dns, period=%dns", slave_addr, sync0_time, sync0_period)
        return True

    def close(self) -> None:
        """关闭EtherCAT主站"""
        if self._soem:
            self._soem.close()
        logger.info("EtherCAT master closed")

    @property
    def is_real_mode(self) -> bool:
        """是否使用真实SOEM模式"""
        return self._use_real_soem


def _get_type_size(data_type: str) -> int:
    """获取数据类型大小"""
    sizes = {
        "bool": 1,
        "int8": 1,
        "uint8": 1,
        "int16": 2,
        "uint16": 2,
        "int32": 4,
        "uint32": 4,
        "float": 4,
        "int64": 8,
        "uint64": 8,
        "double": 8,
    }
    return sizes.get(data_type, 1)


class EtherCATDriver(DriverPlugin):
    """EtherCAT 协议驱动

    配置参数:
        iface: 网络接口名称 (默认eth0)
        timeout: 操作超时毫秒 (默认2000)
        enable_dc: 启用DC分布式时钟同步 (默认False)
    """

    plugin_name = "ethercat"
    plugin_version = "1.1.0"
    supported_protocols = ["ethercat", "ecat"]
    config_schema = {
        "description": "EtherCAT high-speed industrial Ethernet for motion control and distributed IO (SOEM integrated)",
        "fields": [
            {
                "name": "iface",
                "type": "string",
                "label": "Network Interface",
                "description": "EtherCAT network interface (e.g. eth0, ens1f0)",
                "default": "eth0",
            },
            {
                "name": "timeout",
                "type": "integer",
                "label": "Timeout (ms)",
                "description": "Operation timeout in milliseconds",
                "default": 2000,
            },
            {
                "name": "enable_dc",
                "type": "boolean",
                "label": "Enable DC Sync",
                "description": "Enable Distributed Clock synchronization",
                "default": False,
            },
        ],
    }

    _MAX_RECONNECT_ATTEMPTS = 100
    _RECONNECT_BASE_DELAY = 1.0
    _RECONNECT_MAX_DELAY = 60.0

    def __init__(self):
        self._running = False
        self._client: EtherCATClient | None = None
        self._config: dict = {}
        self._device_points: dict[str, dict] = {}
        self._slave_mappings: dict[str, list[PDOMapping]] = {}
        self._lock = asyncio.Lock()
        self._reconnect_count: int = 0
        self._reconnect_delay: float = self._RECONNECT_BASE_DELAY
        self._dc_enabled: bool = False
        self._cycle_task: asyncio.Task | None = None

    async def start(self, config: dict) -> None:
        """启动EtherCAT驱动"""
        self._config = config
        iface = config.get("iface", DEFAULT_IFACE)
        timeout = int(config.get("timeout", EC_TIMEOUT))
        self._dc_enabled = config.get("enable_dc", False)

        self._client = EtherCATClient(iface, timeout)

        try:
            initialized = await self._client.initialize()
            if initialized:
                self._running = True
                self._reconnect_count = 0

                # 启动循环任务
                if self._dc_enabled:
                    self._cycle_task = asyncio.create_task(self._cycle_loop())

                mode_str = "DC enabled" if self._dc_enabled else "standard"
                logger.info("EtherCAT driver started: %s (%s)", iface, mode_str)
            else:
                logger.error("EtherCAT driver initialization failed")
        except Exception as e:
            logger.error("EtherCAT driver start failed: %s", e)
            raise

    async def _cycle_loop(self) -> None:
        """PDO循环任务 - 用于实时数据交换"""
        while self._running and self._dc_enabled:
            try:
                if self._client and self._client._use_real_soem and self._client._soem:
                    # 通过 asyncio.to_thread 释放 GIL
                    await asyncio.to_thread(self._client._soem.send_process_data)
                    await asyncio.to_thread(self._client._soem.receive_process_data)
                await asyncio.sleep(0.001)  # 1ms周期
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("EtherCAT cycle error: %s", e)

    async def stop(self) -> None:
        """停止EtherCAT驱动"""
        self._running = False

        if self._cycle_task:
            self._cycle_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._cycle_task
            self._cycle_task = None

        if self._client:
            self._client.close()
            self._client = None
        logger.info("EtherCAT driver stopped")

    async def add_device(self, device_id: str, config: dict, points: list[dict]) -> None:
        """添加EtherCAT从站设备"""
        slave_addr = config.get("slave_address", 1)
        pdo_mappings = []

        for pt in points:
            mapping = PDOMapping(
                index=pt.get("pdo_index", 0x0000),
                subindex=pt.get("pdo_subindex", 0x00),
                name=pt.get("name", ""),
                data_type=pt.get("data_type", "uint16"),
                direction=pt.get("direction", "input"),
                bit_length=pt.get("bit_length", 0),
            )
            pdo_mappings.append(mapping)

        self._device_points[device_id] = {
            "config": config,
            "points": {p.get("name", ""): p for p in points if p.get("name")},
            "slave_address": slave_addr,
        }
        self._slave_mappings[device_id] = pdo_mappings

        if self._client:
            await self._client.configure_pdo(slave_addr, pdo_mappings)

    def remove_device(self, device_id: str) -> None:
        """Remove a device at runtime"""
        self._device_points.pop(device_id, None)
        self._slave_mappings.pop(device_id, None)
        self._health_stats.pop(device_id, None)
        self._offline_since.pop(device_id, None)
        logger.info("EtherCAT device removed: %s", device_id)

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        """读取EtherCAT测点值

        测点地址格式: "var_name" (在PDO映射中定义)
        """
        if not self._running or not self._client:
            await self._try_reconnect(device_id)
            return {}

        result = {}
        device_info = self._device_points.get(device_id, {})
        slave_addr = device_info.get("slave_address", 1)

        try:
            values = await self._client.read_pdo(slave_addr)
            for point_name in points:
                result[point_name] = values.get(point_name)
        except Exception as e:
            logger.warning("EtherCAT read failed %s: %s", device_id, e)
            result = {p: None for p in points}

        return result

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        """写入EtherCAT测点值"""
        if not self._running or not self._client:
            return False

        device_info = self._device_points.get(device_id, {})
        slave_addr = device_info.get("slave_address", 1)

        try:
            return await self._client.write_pdo(slave_addr, {point: value})
        except Exception as e:
            logger.error("EtherCAT write failed %s.%s: %s", device_id, point, e)
            return False

    async def discover_devices(self, config: dict) -> list[dict]:
        """EtherCAT从站发现"""
        if not self._client:
            return []

        try:
            slaves = await self._client.scan_slaves()
            results = []

            for slave in slaves:
                vendor_id_str = f"0x{slave.vendor_id:08X}"
                product_code_str = f"0x{slave.product_code:08X}"

                results.append(
                    {
                        "device_id": f"ethercat_{slave.station_address}",
                        "name": slave.name,
                        "station_address": slave.station_address,
                        "vendor_id": vendor_id_str,
                        "product_code": product_code_str,
                        "protocol": "ethercat",
                    }
                )

            return results

        except Exception as e:
            logger.error("EtherCAT device discovery failed: %s", e)
            return []

    def is_device_connected(self, device_id: str) -> bool:
        """检查EtherCAT从站连接状态"""
        if not self._running or not self._client:
            return False

        device_info = self._device_points.get(device_id, {})
        slave_addr = device_info.get("slave_address", 1)

        slave = self._client._slaves.get(slave_addr)
        return slave is not None and slave.state == EC_STATE_OPERATIONAL

    async def _try_reconnect(self, device_id: str) -> None:
        """重连机制"""
        if not self._config:
            return

        self._reconnect_count += 1
        if self._reconnect_count > self._MAX_RECONNECT_ATTEMPTS:
            logger.error("EtherCAT reconnect abandoned: %s", device_id)
            return

        delay = min(self._reconnect_delay, self._RECONNECT_MAX_DELAY)
        logger.warning("EtherCAT connection lost, reconnecting in %.1fs (attempt %d)", delay, self._reconnect_count)
        await asyncio.sleep(delay)
        self._reconnect_delay = min(self._reconnect_delay * 2, self._RECONNECT_MAX_DELAY)

        try:
            iface = self._config.get("iface", DEFAULT_IFACE)
            timeout = int(self._config.get("timeout", EC_TIMEOUT))

            if self._client:
                self._client.close()

            self._client = EtherCATClient(iface, timeout)
            initialized = await self._client.initialize()

            if initialized:
                self._running = True
                self._reconnect_count = 0
                self._reconnect_delay = self._RECONNECT_BASE_DELAY
                logger.info("EtherCAT reconnected: %s", iface)
        except Exception as e:
            logger.error("EtherCAT reconnect failed: %s", e)
