"""EtherCAT 驱动单元测试

覆盖修复项:
- ETG.1000.6 命令常量去重与标准化 (替换重复定义的占位符)
- SOEM C 库调用通过 asyncio.to_thread 释放 GIL (避免阻塞事件循环)
  - initialize / scan_slaves / configure_pdo / read_sdo / write_sdo
  - _cycle_loop 周期 send/receive_processdata
- _parse_data / _pack_data 数据序列化正确性
- 模拟模式回退 (无 pysoem 时仍可工作)
"""

from __future__ import annotations

import asyncio
import struct
import sys
import threading

sys.path.insert(0, "src")

from edgelite.drivers.ethercat import (
    EC_CMD_APRD,
    EC_CMD_APWR,
    EC_CMD_APRW,
    EC_CMD_BRD,
    EC_CMD_BRW,
    EC_CMD_BWR,
    EC_CMD_FPRD,
    EC_CMD_FPRW,
    EC_CMD_FPWR,
    EC_CMD_LRD,
    EC_CMD_LRW,
    EC_CMD_LWR,
    EC_CMD_NOP,
    EC_STATE_INIT,
    EC_STATE_OPERATIONAL,
    EC_STATE_PRE_OP,
    EC_STATE_SAFE_OP,
    EtherCATClient,
    EtherCATDriver,
    EtherCATSlave,
    PDOMapping,
    _get_type_size,
    _pack_data,
    _parse_data,
)
from edgelite.drivers.soem_integration import (
    SOEMPdoConfig,
    SOEMSlaveInfo,
)


# ──────────────────────────────────────────────────────────────────
# 1. ETG.1000.6 命令常量正确性 (修复重复定义)
# ──────────────────────────────────────────────────────────────────


class TestEtherCatCommandConstants:
    """ETG.1000.6 标准命令码 — 修复前存在重复定义导致 BRD/BWR/BRW 与 LRD/LWR/LRW 冲突"""

    def test_command_codes_match_standard(self):
        """每个命令码必须匹配 ETG.1000.6 标准"""
        assert EC_CMD_NOP == 0x00
        assert EC_CMD_APRD == 0x01
        assert EC_CMD_APWR == 0x02
        assert EC_CMD_APRW == 0x03
        assert EC_CMD_FPRD == 0x04
        assert EC_CMD_FPWR == 0x05
        assert EC_CMD_FPRW == 0x06
        assert EC_CMD_BRD == 0x07
        assert EC_CMD_BWR == 0x08
        assert EC_CMD_BRW == 0x09
        assert EC_CMD_LRD == 0x0A
        assert EC_CMD_LWR == 0x0B
        assert EC_CMD_LRW == 0x0C

    def test_no_duplicate_command_codes(self):
        """所有命令码必须唯一 — 修复前 EC_CMD_BFRM 被重复赋值 0x06/0x07，
        LRD/LWR/LRW 也存在重复定义"""
        codes = [
            EC_CMD_NOP, EC_CMD_APRD, EC_CMD_APWR, EC_CMD_APRW,
            EC_CMD_FPRD, EC_CMD_FPWR, EC_CMD_FPRW,
            EC_CMD_BRD, EC_CMD_BWR, EC_CMD_BRW,
            EC_CMD_LRD, EC_CMD_LWR, EC_CMD_LRW,
        ]
        assert len(codes) == len(set(codes)), f"命令码存在重复: {codes}"

    def test_command_range_valid(self):
        """命令码必须在 EtherCAT 合法范围 0x00-0x0C"""
        for cmd in (EC_CMD_NOP, EC_CMD_APRD, EC_CMD_APWR, EC_CMD_APRW,
                    EC_CMD_FPRD, EC_CMD_FPWR, EC_CMD_FPRW,
                    EC_CMD_BRD, EC_CMD_BWR, EC_CMD_BRW,
                    EC_CMD_LRD, EC_CMD_LWR, EC_CMD_LRW):
            assert 0x00 <= cmd <= 0x0C

    def test_read_write_command_pairs(self):
        """读/写/读写三件套按 0x01 递增排列 (APRD=1, APWR=2, APRW=3 ...)"""
        # APRD/APWR/APRW
        assert EC_CMD_APWR == EC_CMD_APRD + 1
        assert EC_CMD_APRW == EC_CMD_APRD + 2
        # FPRD/FPWR/FPRW
        assert EC_CMD_FPWR == EC_CMD_FPRD + 1
        assert EC_CMD_FPRW == EC_CMD_FPRD + 2
        # BRD/BWR/BRW
        assert EC_CMD_BWR == EC_CMD_BRD + 1
        assert EC_CMD_BRW == EC_CMD_BRD + 2
        # LRD/LWR/LRW
        assert EC_CMD_LWR == EC_CMD_LRD + 1
        assert EC_CMD_LRW == EC_CMD_LRD + 2


# ──────────────────────────────────────────────────────────────────
# 2. 状态机常量
# ──────────────────────────────────────────────────────────────────


class TestEtherCatStateConstants:
    """EtherCAT 状态机常量"""

    def test_state_values(self):
        assert EC_STATE_INIT == 0x01
        assert EC_STATE_PRE_OP == 0x02
        assert EC_STATE_SAFE_OP == 0x04
        assert EC_STATE_OPERATIONAL == 0x08

    def test_states_bit_flags(self):
        """状态码必须是单 bit 标志 (INIT/PRE_OP/SAFE_OP/OP)"""
        for state in (EC_STATE_INIT, EC_STATE_PRE_OP, EC_STATE_SAFE_OP, EC_STATE_OPERATIONAL):
            assert state & (state - 1) == 0, f"状态 0x{state:02X} 不是单 bit"


# ──────────────────────────────────────────────────────────────────
# 3. 数据序列化: _pack_data / _parse_data / _get_type_size
# ──────────────────────────────────────────────────────────────────


class TestPackParseData:
    """EtherCAT 数据打包/解析往返一致性"""

    def test_pack_unpack_bool(self):
        for v in (True, False):
            packed = _pack_data(v, "bool")
            assert _parse_data(packed, 0, "bool") == bool(v)

    def test_pack_unpack_uint8(self):
        for v in (0, 1, 127, 200, 255):
            packed = _pack_data(v, "uint8")
            assert _parse_data(packed, 0, "uint8") == v

    def test_pack_unpack_int8(self):
        for v in (-128, -1, 0, 1, 127):
            packed = _pack_data(v, "int8")
            assert _parse_data(packed, 0, "int8") == v

    def test_pack_unpack_uint16(self):
        for v in (0, 1, 256, 65535):
            packed = _pack_data(v, "uint16")
            assert _parse_data(packed, 0, "uint16") == v

    def test_pack_unpack_int16(self):
        for v in (-32768, -1, 0, 32767):
            packed = _pack_data(v, "int16")
            assert _parse_data(packed, 0, "int16") == v

    def test_pack_unpack_uint32(self):
        for v in (0, 1, 70000, 4294967295):
            packed = _pack_data(v, "uint32")
            assert _parse_data(packed, 0, "uint32") == v

    def test_pack_unpack_int32(self):
        for v in (-2147483648, -1, 0, 2147483647):
            packed = _pack_data(v, "int32")
            assert _parse_data(packed, 0, "int32") == v

    def test_pack_unpack_float(self):
        for v in (0.0, 1.5, -3.25, 100.0):
            packed = _pack_data(v, "float")
            result = _parse_data(packed, 0, "float")
            assert abs(result - v) < 1e-6

    def test_parse_data_offset(self):
        """_parse_data 必须从指定偏移读取"""
        buf = b"\x00\x00\xFF\x00"  # offset=2 处是 0xFF
        assert _parse_data(buf, 2, "uint8") == 0xFF

    def test_parse_data_short_buffer_returns_zero(self):
        """缓冲不足 (offset 有效但数据不足) 时返回 0 而非抛异常"""
        # offset=1 有效 (len >= 1), 但 uint16 需要 offset+2=3 字节, 不足则返回 0
        assert _parse_data(b"\x00\x00", 1, "uint16") == 0

    def test_parse_data_offset_beyond_buffer_returns_none(self):
        """offset 超出缓冲长度时返回 None"""
        assert _parse_data(b"\x00", 4, "uint16") is None

    def test_parse_data_bool_bit_addressing(self):
        """bool 类型支持 bit_length 位寻址"""
        # byte 0 bit 3 = 1
        buf = bytes([0x08])
        assert _parse_data(buf, 0, "bool", bit_length=3) is True
        # bit_length=0 时回退到原始字节判读: 0x08 非零 → True
        assert _parse_data(buf, 0, "bool", bit_length=0) is True
        # 零字节 → False
        assert _parse_data(bytes([0x00]), 0, "bool", bit_length=0) is False

    def test_pack_data_unknown_type_falls_back(self):
        """未知类型回退到单字节打包"""
        packed = _pack_data(0x42, "unknown_type")
        assert packed == bytes([0x42])


class TestGetTypeSize:
    """_get_type_size 返回值"""

    def test_known_sizes(self):
        assert _get_type_size("bool") == 1
        assert _get_type_size("int8") == 1
        assert _get_type_size("uint8") == 1
        assert _get_type_size("int16") == 2
        assert _get_type_size("uint16") == 2
        assert _get_type_size("int32") == 4
        assert _get_type_size("uint32") == 4
        assert _get_type_size("float") == 4
        assert _get_type_size("int64") == 8
        assert _get_type_size("double") == 8

    def test_unknown_type_defaults_to_1(self):
        assert _get_type_size("nonexistent") == 1


# ──────────────────────────────────────────────────────────────────
# 4. SOEM GIL 释放验证 — 核心修复
#    使用记录线程 ID 的 FakeSOEM 验证调用被派发到工作线程
# ──────────────────────────────────────────────────────────────────


class _ThreadRecordingSOEM:
    """伪 SOEM 上下文 — 记录每次调用所在的线程 ID

    用于验证 EtherCATClient 的 async 方法确实通过 asyncio.to_thread
    将 SOEM C 库调用派发到默认线程池 (而非事件循环线程)。
    """

    def __init__(self):
        self.main_thread_id = threading.get_ident()
        self.initialize_thread: int | None = None
        self.scan_slaves_thread: int | None = None
        self.configure_pdo_thread: int | None = None
        self.read_sdo_thread: int | None = None
        self.write_sdo_thread: int | None = None
        self.send_pd_thread: int | None = None
        self.receive_pd_thread: int | None = None
        self.configure_pdo_args: tuple | None = None
        self.read_sdo_args: tuple | None = None
        self.write_sdo_args: tuple | None = None
        self._slaves: list[SOEMSlaveInfo] = [
            SOEMSlaveInfo(
                position=1,
                vendor_id=0xA,
                product_code=0xB,
                revision_number=0xC,
                device_type=0xD,
                name="FakeSlave",
                state=EC_STATE_PRE_OP,
            )
        ]

    def initialize(self) -> bool:
        self.initialize_thread = threading.get_ident()
        return True

    def scan_slaves(self) -> list[SOEMSlaveInfo]:
        self.scan_slaves_thread = threading.get_ident()
        return list(self._slaves)

    def configure_pdo(self, slave_position: int, mappings: list[SOEMPdoConfig]) -> bool:
        self.configure_pdo_thread = threading.get_ident()
        self.configure_pdo_args = (slave_position, mappings)
        return True

    def read_sdo(self, slave: int, index: int, subindex: int, data_type: str = "uint32"):
        self.read_sdo_thread = threading.get_ident()
        self.read_sdo_args = (slave, index, subindex, data_type)
        return 0xDEADBEEF

    def write_sdo(self, slave: int, index: int, subindex: int, value, data_type: str = "uint32") -> bool:
        self.write_sdo_thread = threading.get_ident()
        self.write_sdo_args = (slave, index, subindex, value, data_type)
        return True

    def send_process_data(self) -> int:
        self.send_pd_thread = threading.get_ident()
        return 1

    def receive_process_data(self) -> int:
        self.receive_pd_thread = threading.get_ident()
        return 3  # WKC=3 (3 个从站都响应)

    def close(self) -> None:
        pass


def _make_client_with_fake_soem() -> tuple[EtherCATClient, _ThreadRecordingSOEM]:
    """构造 EtherCATClient 并注入伪 SOEM，跳过真实初始化"""
    client = EtherCATClient.__new__(EtherCATClient)
    client._iface = "lo"
    client._timeout = 100
    fake = _ThreadRecordingSOEM()
    client._soem = fake
    client._slaves = {}
    client._pdo_mappings = {}
    client._output_size = 0
    client._input_size = 0
    client._initialized = True
    client._use_real_soem = True
    return client, fake


class TestSoemGilRelease:
    """验证所有 SOEM C 库调用通过 asyncio.to_thread 释放 GIL"""

    async def test_initialize_runs_in_worker_thread(self, monkeypatch):
        """initialize() 内部创建 SOEMContext 并调用其 initialize()
        通过 monkeypatch 替换 SOEMContext 工厂，注入记录线程的伪对象"""
        from edgelite.drivers import ethercat as ec_mod

        fake = _ThreadRecordingSOEM()

        # 捕获 initialize 调用前的线程 ID (事件循环线程)
        main_tid = threading.get_ident()

        def _fake_factory(iface: str, timeout: int) -> _ThreadRecordingSOEM:
            return fake

        monkeypatch.setattr(ec_mod, "SOEMContext", _fake_factory)

        client = EtherCATClient("lo", 100)
        await client.initialize()
        assert fake.initialize_thread is not None
        assert fake.initialize_thread != main_tid, \
            "initialize 必须在工作线程执行，而非事件循环线程"

    async def test_scan_slaves_runs_in_worker_thread(self):
        client, fake = _make_client_with_fake_soem()
        slaves = await client.scan_slaves()
        assert fake.scan_slaves_thread is not None
        assert fake.scan_slaves_thread != fake.main_thread_id
        # 验证返回数据完整
        assert len(slaves) == 1
        assert slaves[0].station_address == 1
        assert slaves[0].vendor_id == 0xA
        assert slaves[0].name == "FakeSlave"
        # 验证同时缓存到 _slaves
        assert 1 in client._slaves

    async def test_configure_pdo_runs_in_worker_thread(self):
        client, fake = _make_client_with_fake_soem()
        # 预置从站
        client._slaves[1] = EtherCATSlave(
            station_address=1, vendor_id=0, product_code=0,
            revision_number=0, name="S1",
        )
        mappings = [
            PDOMapping(index=0x1A00, subindex=0x01, name="speed",
                       data_type="uint16", direction="input"),
            PDOMapping(index=0x1600, subindex=0x01, name="torque",
                       data_type="int16", direction="output"),
        ]
        ok = await client.configure_pdo(1, mappings)
        assert ok is True
        assert fake.configure_pdo_thread is not None
        assert fake.configure_pdo_thread != fake.main_thread_id
        # 验证 SOEM 收到正确的 mapping 转换
        slave_pos, soem_maps = fake.configure_pdo_args
        assert slave_pos == 1
        assert len(soem_maps) == 2
        assert isinstance(soem_maps[0], SOEMPdoConfig)
        assert soem_maps[0].name == "speed"
        assert soem_maps[0].data_type == "uint16"
        # 验证 PDO 大小计算
        assert client._output_size == 2  # int16
        assert client._input_size == 2   # uint16

    async def test_read_sdo_runs_in_worker_thread(self):
        client, fake = _make_client_with_fake_soem()
        result = await client.read_sdo(slave_addr=1, index=0x6040, subindex=0x00)
        assert result == 0xDEADBEEF
        assert fake.read_sdo_thread is not None
        assert fake.read_sdo_thread != fake.main_thread_id
        # 验证参数透传
        assert fake.read_sdo_args == (1, 0x6040, 0x00, "uint32")

    async def test_write_sdo_runs_in_worker_thread(self):
        client, fake = _make_client_with_fake_soem()
        ok = await client.write_sdo(
            slave_addr=1, index=0x6040, subindex=0x00,
            value=0x000F, data_type="uint16",
        )
        assert ok is True
        assert fake.write_sdo_thread is not None
        assert fake.write_sdo_thread != fake.main_thread_id
        assert fake.write_sdo_args == (1, 0x6040, 0x00, 0x000F, "uint16")

    async def test_all_soem_calls_do_not_block_event_loop(self):
        """并发执行多个 SOEM 调用，验证它们不会串行阻塞事件循环

        如果 SOEM 调用是同步的 (未 to_thread)，3 个 sleep(0.05) 的
        伪调用累计 150ms；to_thread 后并发执行应在 ~50ms 完成。
        """
        client, fake = _make_client_with_fake_soem()
        client._slaves[1] = EtherCATSlave(
            station_address=1, vendor_id=0, product_code=0,
            revision_number=0, name="S1",
        )

        # 包装伪 SOEM 让每次调用 sleep 50ms 模拟 C 库阻塞
        import time

        orig_initialize = fake.initialize
        orig_scan = fake.scan_slaves
        orig_configure = fake.configure_pdo

        def slow_initialize():
            time.sleep(0.05)
            return orig_initialize()

        def slow_scan():
            time.sleep(0.05)
            return orig_scan()

        def slow_configure(sp, mp):
            time.sleep(0.05)
            return orig_configure(sp, mp)

        fake.initialize = slow_initialize
        fake.scan_slaves = slow_scan
        fake.configure_pdo = slow_configure

        loop = asyncio.get_running_loop()
        t0 = loop.time()
        # 并发执行 3 个会阻塞的 SOEM 调用
        await asyncio.gather(
            client.initialize(),
            client.scan_slaves(),
            client.configure_pdo(1, []),
        )
        elapsed = loop.time() - t0
        # to_thread 后并发: ~50ms; 同步阻塞: ~150ms. 阈值 100ms 区分.
        assert elapsed < 0.10, (
            f"SOEM 调用未并发执行 (耗时 {elapsed:.3f}s)，"
            "可能未通过 asyncio.to_thread 释放 GIL"
        )


# ──────────────────────────────────────────────────────────────────
# 5. _cycle_loop 周期任务 — 验证调用 send/receive_processdata
# ──────────────────────────────────────────────────────────────────


class TestCycleLoopProcessData:
    """验证 DC 同步模式下 _cycle_loop 调用 SOEM 过程数据交换"""

    async def test_cycle_loop_calls_process_data_in_worker_thread(self):
        client, fake = _make_client_with_fake_soem()
        driver = EtherCATDriver.__new__(EtherCATDriver)
        driver._running = True
        driver._client = client
        driver._dc_enabled = True
        driver._cycle_task = None
        driver._config = {}
        driver._device_points = {}
        driver._slave_mappings = {}
        driver._lock = asyncio.Lock()
        driver._reconnect_count = 0
        driver._reconnect_delay = 1.0
        driver._dc_enabled = True

        # 启动 cycle loop，运行 3 个周期后停止
        task = asyncio.ensure_future(driver._cycle_loop())
        # 等待至少 2 个 1ms 周期完成
        await asyncio.sleep(0.005)
        driver._running = False
        # 等待任务退出
        await asyncio.wait_for(task, timeout=1.0)

        assert fake.send_pd_thread is not None, "send_process_data 未被调用"
        assert fake.receive_pd_thread is not None, "receive_process_data 未被调用"
        assert fake.send_pd_thread != fake.main_thread_id, \
            "send_process_data 必须在工作线程执行"
        assert fake.receive_pd_thread != fake.main_thread_id, \
            "receive_process_data 必须在工作线程执行"

    async def test_cycle_loop_skips_when_not_real_soem(self):
        """模拟模式下 _cycle_loop 不应调用 SOEM (无 _use_real_soem)"""
        client = EtherCATClient.__new__(EtherCATClient)
        client._soem = None
        client._use_real_soem = False
        driver = EtherCATDriver.__new__(EtherCATDriver)
        driver._running = True
        driver._client = client
        driver._dc_enabled = True

        task = asyncio.ensure_future(driver._cycle_loop())
        await asyncio.sleep(0.005)
        driver._running = False
        await asyncio.wait_for(task, timeout=1.0)
        # 无异常即通过 — 模拟模式下应静默跳过 SOEM 调用

    async def test_cycle_loop_handles_cancellation(self):
        """_cycle_loop 必须正确响应 CancelledError (stop() 取消)"""
        client, _ = _make_client_with_fake_soem()
        driver = EtherCATDriver.__new__(EtherCATDriver)
        driver._running = True
        driver._client = client
        driver._dc_enabled = True

        task = asyncio.ensure_future(driver._cycle_loop())
        await asyncio.sleep(0.002)
        task.cancel()
        # 应当在 1s 内退出而不抛异常
        try:
            await asyncio.wait_for(task, timeout=1.0)
        except asyncio.CancelledError:
            pass  # 正常退出路径


# ──────────────────────────────────────────────────────────────────
# 6. 模拟模式回退 (无 pysoem 可用时)
# ──────────────────────────────────────────────────────────────────


class TestSimulationModeFallback:
    """无 pysoem 时驱动必须能正常工作 (降级到模拟数据)"""

    async def test_scan_slaves_simulation_returns_beckhoff_slaves(self):
        client = EtherCATClient.__new__(EtherCATClient)
        client._iface = "lo"
        client._timeout = 100
        client._soem = None
        client._slaves = {}
        client._pdo_mappings = {}
        client._output_size = 0
        client._input_size = 0
        client._initialized = True
        client._use_real_soem = False  # 模拟模式

        slaves = await client.scan_slaves()
        assert len(slaves) == 2
        # 模拟数据: Beckhoff EK1100 + EL4001
        assert slaves[0].name == "EK1100 (Coupler)"
        assert slaves[0].vendor_id == 0x00000002
        assert slaves[1].name == "EL4001 (AO 4ch)"
        # 验证缓存
        assert 1 in client._slaves
        assert 2 in client._slaves

    async def test_read_sdo_simulation_returns_none(self):
        client = EtherCATClient.__new__(EtherCATClient)
        client._soem = None
        client._use_real_soem = False
        result = await client.read_sdo(1, 0x6040, 0x00)
        assert result is None

    async def test_write_sdo_simulation_returns_true(self):
        client = EtherCATClient.__new__(EtherCATClient)
        client._soem = None
        client._use_real_soem = False
        ok = await client.write_sdo(1, 0x6040, 0x00, 1, "uint16")
        assert ok is True


# ──────────────────────────────────────────────────────────────────
# 7. PDO 读写 — 验证 _slaves 缓存和 mapping 解析
# ──────────────────────────────────────────────────────────────────


class TestPdoReadWrite:
    """PDO 数据读写 (基于本地缓存，不涉及 SOEM)"""

    async def test_write_pdo_packs_output_data(self):
        client = EtherCATClient.__new__(EtherCATClient)
        client._slaves = {
            1: EtherCATSlave(
                station_address=1, vendor_id=0, product_code=0,
                revision_number=0, name="S1",
            )
        }
        client._pdo_mappings = {
            1: [
                PDOMapping(index=0x1600, subindex=1, name="ctrl",
                           data_type="uint16", direction="output"),
                PDOMapping(index=0x1601, subindex=1, name="speed",
                           data_type="int16", direction="output"),
            ]
        }
        client._output_size = 4
        ok = await client.write_pdo(1, {"ctrl": 0x000F, "speed": -100})
        assert ok is True
        out = client._slaves[1].outputs
        # uint16 0x000F 小端 = 0F 00
        assert out[0] == 0x0F
        assert out[1] == 0x00
        # int16 -100 小端 = 9C FF
        assert out[2] == 0x9C
        assert out[3] == 0xFF

    async def test_read_pdo_parses_input_data(self):
        client = EtherCATClient.__new__(EtherCATClient)
        slave = EtherCATSlave(
            station_address=1, vendor_id=0, product_code=0,
            revision_number=0, name="S1",
        )
        # 预置输入: uint16=0x1234, int16=-1, float=1.5
        slave.inputs = struct.pack("<Hh", 0x1234, -1) + struct.pack("<f", 1.5)
        client._slaves = {1: slave}
        client._pdo_mappings = {
            1: [
                PDOMapping(index=0x1A00, subindex=1, name="status",
                           data_type="uint16", direction="input"),
                PDOMapping(index=0x1A01, subindex=1, name="temp",
                           data_type="int16", direction="input"),
                PDOMapping(index=0x1A02, subindex=1, name="pressure",
                           data_type="float", direction="input"),
            ]
        }
        result = await client.read_pdo(1)
        assert result["status"] == 0x1234
        assert result["temp"] == -1
        assert abs(result["pressure"] - 1.5) < 1e-6

    async def test_read_pdo_unknown_slave_returns_empty(self):
        client = EtherCATClient.__new__(EtherCATClient)
        client._slaves = {}
        result = await client.read_pdo(99)
        assert result == {}

    async def test_write_pdo_unknown_slave_returns_false(self):
        client = EtherCATClient.__new__(EtherCATClient)
        client._slaves = {}
        client._output_size = 0
        ok = await client.write_pdo(99, {"x": 1})
        assert ok is False

    async def test_configure_pdo_unknown_slave_returns_false(self):
        client = EtherCATClient.__new__(EtherCATClient)
        client._slaves = {}
        client._pdo_mappings = {}
        client._output_size = 0
        client._input_size = 0
        client._soem = None
        client._use_real_soem = False
        ok = await client.configure_pdo(99, [])
        assert ok is False


# ──────────────────────────────────────────────────────────────────
# 8. PDOMapping / EtherCATSlave 数据类
# ──────────────────────────────────────────────────────────────────


class TestDataclasses:
    """数据类默认值与字段"""

    def test_pdo_mapping_defaults(self):
        m = PDOMapping(index=0x1A00, subindex=1, name="x", data_type="uint16", direction="input")
        assert m.bit_length == 0

    def test_ethercat_slave_defaults(self):
        s = EtherCATSlave(
            station_address=1, vendor_id=2, product_code=3,
            revision_number=4, name="S",
        )
        assert s.alias == 0
        assert s.state == EC_STATE_INIT
        assert s.outputs == b""
        assert s.inputs == b""
