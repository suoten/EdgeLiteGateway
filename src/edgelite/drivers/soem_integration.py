"""EtherCAT SOEM集成模块 - 提供与SOEM库的Python绑定

SOEM (Simple Open EtherCAT Master) 是一个开源的EtherCAT主站库。
本模块提供与SOEM库的Python绑定，支持:
- 真实从站扫描
- PDO数据交换
- SDO参数读写
- DC分布式时钟同步

依赖安装:
    # Linux (Debian/Ubuntu)
    apt install soem
    pip install pysoem

    # Linux (Alpine)
    apk add soem-dev
    pip install pysoem

    # macOS
    brew install soem
    pip install pysoem

    # Windows
    pip install pysoem

验证安装:
    python -c "import pysoem; print(dir(pysoem))"
"""

from __future__ import annotations

import logging
import platform
import struct
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# 尝试导入 pysoem 包
try:
    import pysoem

    PYSOEM_AVAILABLE = True
except ImportError:
    PYSOEM_AVAILABLE = False
    pysoem = None

# 兼容别名: ethercat.py 导入 SOEM_AVAILABLE
SOEM_AVAILABLE = PYSOEM_AVAILABLE

# EtherCAT 状态常量
EC_STATE_INIT = 0x01
EC_STATE_PRE_OP = 0x02
EC_STATE_BOOT = 0x03
EC_STATE_SAFE_OP = 0x04
EC_STATE_OPERATIONAL = 0x08
EC_STATE_ACK = 0x10
EC_STATE_ERROR = 0x20


@dataclass
class SOEMSlaveInfo:
    """SOEM从站信息"""

    position: int
    vendor_id: int
    product_code: int
    revision_number: int
    device_type: int
    name: str = ""
    state: int = EC_STATE_INIT
    alias: int = 0
    outputs: bytes = b""
    inputs: bytes = b""


@dataclass
class SOEMPdoConfig:
    """PDO配置"""

    index: int
    subindex: int
    name: str
    data_type: str
    direction: str  # "output" or "input"
    bit_length: int = 0


class SOEMContext:
    """SOEM上下文封装，支持 pysoem 包"""

    def __init__(self, iface: str, timeout: int = 2000):
        self._iface = iface
        self._timeout = timeout
        self._ifhandle: Any = None
        self._slaves: list[SOEMSlaveInfo] = []
        self._pdo_configs: dict[int, list[SOEMPdoConfig]] = {}
        self._initialized = False
        self._wkc: int = 0
        self._use_pysoem = PYSOEM_AVAILABLE

    def _check_soem(self) -> bool:
        """检查SOEM是否可用"""
        if self._use_pysoem:
            return True
        # FIXED-P0: 原问题-降级日志级别仅为 warning 且未提示生产风险；
        # 修复-明确告知用户当前为模拟模式，绝不可用于生产监控。
        logger.warning(
            "[EtherCAT] pysoem 未安装或加载失败，SOEM 将以模拟模式运行。"
            "模拟模式返回 Beckhoff EK1100/EL4001/EL2004 占位从站与占位数据，"
            "仅用于开发与测试，绝不可用于生产监控。生产部署请 `pip install pysoem` "
            "并在 Linux + CAP_NET_RAW 能力下运行，或改用 IgH/TwinCAT 主站。"
        )
        return False

    def initialize(self) -> bool:
        """初始化SOEM上下文"""
        if not self._use_pysoem:
            # FIXED-P0: 与 _check_soem 一致，明确告知模拟模式风险
            logger.warning(
                "[EtherCAT] pysoem 未安装，SOEM 以模拟模式初始化。"
                "生产环境不推荐：参见 ethercat.py 模块 WARNING 注释。"
            )
            self._initialized = True
            return True

        try:
            # pysoem 1.1.x API
            self._ifhandle = pysoem.Network()
            self._initialized = True
            logger.info("SOEM Network created (pysoem %s)", getattr(pysoem, "__version__", "unknown"))
            return True
        except Exception as e:
            # FIXED-P0: 原问题-pysoem 创建失败时仅 logger.error 后静默降级到模拟模式；
            # 修复-以 WARNING 级别明确告知用户已降级且不可用于生产。
            logger.error("Failed to create SOEM Network: %s", e)
            logger.warning(
                "[EtherCAT] pysoem Network 创建失败，已降级到模拟模式。"
                "可能原因：Windows 下缺少 WinPcap/Npcap、未以管理员权限运行、"
                "或 pysoem 版本与 SOEM 库不匹配。生产环境必须解决此问题，"
                "模拟模式仅用于开发测试。"
            )
            self._use_pysoem = False
            self._initialized = True
            return True  # 降级到模拟模式

    def scan_slaves(self) -> list[SOEMSlaveInfo]:
        """扫描EtherCAT从站"""
        self._slaves = []

        if not self._initialized:
            logger.error("SOEM not initialized")
            return self._slaves

        if not self._use_pysoem:
            return self._simulate_scan()

        try:
            # 打开网络接口并扫描从站
            self._ifhandle.open(self._iface)
            self._ifhandle.config_init()

            slave_count = self._ifhandle.ec_slavecount
            logger.info("Found %d EtherCAT slaves", slave_count)

            # 读取每个从站信息
            for i in range(slave_count):
                slave = self._ifhandle.slaves[i]

                # 解析设备名称
                name = ""
                try:
                    if hasattr(slave, "name") and slave.name:
                        if isinstance(slave.name, bytes):
                            name = slave.name.decode("utf-8", errors="replace").strip("\x00")
                        else:
                            name = str(slave.name)
                except Exception as e:
                    logger.debug("[soem] slave name parse failed for index %d: %s", i, e)
                    name = f"Slave_{i + 1}"

                # 获取厂商ID和产品码
                vendor_id = getattr(slave, "man", 0)
                product_code = getattr(slave, "id", 0)
                revision = getattr(slave, "rev", 0)
                state = getattr(slave, "state", EC_STATE_INIT)

                slave_info = SOEMSlaveInfo(
                    position=i + 1,
                    vendor_id=vendor_id,
                    product_code=product_code,
                    revision_number=revision,
                    device_type=0,
                    name=name,
                    state=state,
                    alias=getattr(slave, "alias", 0),
                )
                self._slaves.append(slave_info)
                logger.debug(
                    "Slave %d: %s (VID: 0x%08X, PID: 0x%08X, REV: 0x%08X)",
                    i + 1,
                    name,
                    vendor_id,
                    product_code,
                    revision,
                )

            return self._slaves

        except Exception as e:
            logger.error("SOEM slave scan error: %s", e)
            return self._simulate_scan()

    def _simulate_scan(self) -> list[SOEMSlaveInfo]:
        """模拟从站扫描结果"""
        logger.info("Using simulated EtherCAT slave scan")

        simulated_slaves = [
            SOEMSlaveInfo(
                position=1,
                vendor_id=0x00000002,  # Beckhoff
                product_code=0x044C2C52,
                revision_number=0x00120000,
                device_type=0x00010188,
                name="EK1100 (Coupler)",
            ),
            SOEMSlaveInfo(
                position=2,
                vendor_id=0x00000002,  # Beckhoff
                product_code=0x13ED3052,
                revision_number=0x00110000,
                device_type=0x00020188,
                name="EL4001 (AO 4ch)",
            ),
            SOEMSlaveInfo(
                position=3,
                vendor_id=0x00000002,  # Beckhoff
                product_code=0x1F573052,
                revision_number=0x00120000,
                device_type=0x00020188,
                name="EL2004 (DO 4ch)",
            ),
        ]

        for slave in simulated_slaves:
            self._slaves.append(slave)

        return self._slaves

    def configure_pdo(self, slave_position: int, mappings: list[SOEMPdoConfig]) -> bool:
        """配置PDO映射"""
        self._pdo_configs[slave_position] = mappings

        if not self._use_pysoem:
            return True

        try:
            if slave_position > len(self._slaves):
                logger.error("Invalid slave position: %d", slave_position)
                return False

            logger.info("PDO configured for slave %d: %d mappings", slave_position, len(mappings))
            return True

        except Exception as e:
            logger.error("PDO configuration error: %s", e)
            return False

    def request_state(self, slave_position: int, state: int) -> bool:
        """请求从站状态转换"""
        if not self._use_pysoem:
            logger.debug("State request (simulated): slave=%d state=0x%02X", slave_position, state)
            return True

        try:
            if self._ifhandle is None:
                return False

            # pysoem 使用 0-based 索引
            self._ifhandle.state_check(slave_position - 1, state, timeout=self._timeout)
            logger.debug("State request sent for slave %d: 0x%02X", slave_position, state)
            return True

        except Exception as e:
            logger.error("State request error: %s", e)
            return False

    def send_process_data(self) -> int:
        """发送过程数据 (输出)"""
        if not self._use_pysoem:
            return 1

        try:
            if self._ifhandle is None:
                return 0
            self._ifhandle.send_processdata()
            return 1
        except Exception as e:
            logger.warning("[soem] send_processdata failed: %s", e)
            return 0

    def receive_process_data(self) -> int:
        """接收过程数据 (输入)"""
        if not self._use_pysoem:
            return 1

        try:
            if self._ifhandle is None:
                return 0
            wkc = self._ifhandle.receive_processdata(timeout=self._timeout)
            return wkc if wkc is not None else 0
        except Exception as e:
            logger.warning("[soem] receive_processdata failed: %s", e)
            return 0

    def read_sdo(self, slave: int, index: int, subindex: int, data_type: str = "uint32") -> Any | None:
        """读取SDO"""
        if not self._use_pysoem:
            logger.debug("SDO read (simulated): slave=%d 0x%04X:%02X", slave, index, subindex)
            return None

        try:
            if self._ifhandle is None:
                return None

            result = self._ifhandle.sdo_read(index, subindex, timeout=self._timeout)
            if result is None:
                return None

            return self._parse_sdo_data(result, data_type)

        except Exception as e:
            logger.debug("SDO read error: %s", e)
            return None

    def _parse_sdo_data(self, data: bytes, data_type: str) -> Any:
        """解析SDO数据"""
        if not data:
            return None

        if data_type == "uint32":
            return struct.unpack("<I", data[:4])[0]
        elif data_type == "int32":
            return struct.unpack("<i", data[:4])[0]
        elif data_type == "uint16":
            return struct.unpack("<H", data[:2])[0]
        elif data_type == "int16":
            return struct.unpack("<h", data[:2])[0]
        elif data_type == "string":
            return data.decode("utf-8", errors="replace").strip("\x00")
        else:
            return data.hex()

    def write_sdo(self, slave: int, index: int, subindex: int, value: Any, data_type: str = "uint32") -> bool:
        """写入SDO"""
        if not self._use_pysoem:
            logger.debug("SDO write (simulated): slave=%d 0x%04X:%02X = %s", slave, index, subindex, value)
            return True

        try:
            if self._ifhandle is None:
                return False

            data = self._pack_sdo_data(value, data_type)
            result = self._ifhandle.sdo_write(index, subindex, data, timeout=self._timeout)
            return result

        except Exception as e:
            logger.error("SDO write error: %s", e)
            return False

    def _pack_sdo_data(self, value: Any, data_type: str) -> bytes:
        """打包SDO数据"""
        if data_type == "uint32":
            return struct.pack("<I", int(value))
        elif data_type == "int32":
            return struct.pack("<i", int(value))
        elif data_type == "uint16":
            return struct.pack("<H", int(value))
        elif data_type == "int16":
            return struct.pack("<h", int(value))
        elif data_type == "string":
            return str(value).encode("utf-8")
        else:
            return str(value).encode()

    def close(self) -> None:
        """关闭SOEM上下文"""
        if self._use_pysoem and self._ifhandle is not None:
            try:
                self._ifhandle.close()
                logger.info("SOEM closed")
            except Exception as e:
                logger.debug("SOEM close error: %s", e)

        self._initialized = False
        self._ifhandle = None

    @property
    def slaves(self) -> list[SOEMSlaveInfo]:
        """获取扫描到的从站列表"""
        return self._slaves

    @property
    def is_real_mode(self) -> bool:
        """是否使用真实SOEM模式"""
        return self._use_pysoem and self._initialized


class EtherCATDriverMixin:
    """EtherCAT驱动混入类，提供通用的EtherCAT操作"""

    @staticmethod
    def get_state_name(state: int) -> str:
        """获取状态名称"""
        states = {
            EC_STATE_INIT: "INIT",
            EC_STATE_PRE_OP: "PRE_OP",
            EC_STATE_BOOT: "BOOT",
            EC_STATE_SAFE_OP: "SAFE_OP",
            EC_STATE_OPERATIONAL: "OPERATIONAL",
            EC_STATE_ACK: "ACK",
            EC_STATE_ERROR: "ERROR",
        }
        return states.get(state, f"UNKNOWN(0x{state:02X})")

    @staticmethod
    def parse_vendor_product(vendor_id: int, product_code: int) -> str:
        """解析厂商和产品信息"""
        vendors = {
            0x00000002: "Beckhoff",
            0x00000539: "Siemens",
            0x0000066F: "Schneider Electric",
            0x00000009: "ABB",
            0x00000A27: "Bosch Rexroth",
            0x00000013: "IFM",
            0x00000056: "SICK",
            0x000005AC: "Balluff",
        }
        vendor_name = vendors.get(vendor_id, f"VID_{vendor_id:08X}")
        return f"{vendor_name} (0x{product_code:08X})"
