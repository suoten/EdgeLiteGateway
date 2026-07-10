"""FINS 协议驱动单元测试

覆盖 Task #16 P1 修复:
  FINS UDP 重传机制 — UDP 模式下 FINS 命令丢包时自动重传

  原问题: UDPFinsConnection.execute_fins_command_frame 单次 sendto + recvfrom，
          UDP 丢包时 recvfrom 抛 socket.timeout 直接失败，无应用层重传。
          工业现场 UDP 丢包率可达 0.1%-1%，导致 FINS 读写在 UDP 模式下频繁失败。

  修复: _wrap_udp_retransmission 方法为 UDPFinsConnection 注入重传逻辑:
    - socket.timeout (丢包) 时按指数退避重传 (10ms, 20ms, 40ms... 上限 200ms)
    - 最多重传 _udp_max_retries 次 (默认 3 次，总尝试 4 次)
    - 非 timeout 的 OSError 不重传 (连接重置等不可恢复错误)
    - 支持 udp_retries=0 禁用重传
    - 重传逻辑在 _do_connect 和 standby client 初始化时注入
"""

from __future__ import annotations

import socket
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, "src")

from edgelite.drivers.fins import OmronFinsDriver


def _make_driver(udp_retries: int = 3) -> OmronFinsDriver:
    """构造 OmronFinsDriver 实例 (绕过 __init__ 的复杂依赖)，仅设置测试所需属性。"""
    driver = OmronFinsDriver.__new__(OmronFinsDriver)
    driver._udp_max_retries = udp_retries
    driver._is_udp = True
    return driver


class _FakeUdpFinsConnection:
    """模拟 fins.udp.UDPFinsConnection — 捕获 execute_fins_command_frame 调用。

    可配置 original_execute 的行为:
    - 返回固定响应
    - 抛 socket.timeout 模拟丢包
    - 抛 OSError 模拟连接错误
    - 按序列返回不同结果 (前N次超时，第N+1次成功)
    """

    def __init__(self):
        self.ip_address = "192.168.1.100"
        self.fins_port = 9600
        self.BUFFER_SIZE = 4096
        self.fins_socket = MagicMock()
        self._execute_call_count = 0
        self._timeout_sequence: list[bool] = []  # True=timeout, False=success
        self._response = b"\x00" * 16  # 默认响应帧

    def execute_fins_command_frame(self, fins_command_frame: bytes):
        self._execute_call_count += 1
        if self._execute_call_count <= len(self._timeout_sequence):
            if self._timeout_sequence[self._execute_call_count - 1]:
                raise TimeoutError("simulated UDP packet loss")
        return self._response

    def set_timeout_sequence(self, *timeouts: bool) -> None:
        """设置前N次调用是否超时。True=timeout, False=success"""
        self._timeout_sequence = list(timeouts)


# ══════════════════════════════════════════════════════════════════════
# 1. _wrap_udp_retransmission 核心逻辑
# ══════════════════════════════════════════════════════════════════════


class TestUdpRetransmissionSuccess:
    """UDP 重传成功路径 — 丢包后重传获得响应"""

    def test_first_attempt_success_no_retrans(self):
        """首次成功 → 不重传，execute 仅调用 1 次"""
        driver = _make_driver(udp_retries=3)
        client = _FakeUdpFinsConnection()
        driver._wrap_udp_retransmission(client)

        result = client.execute_fins_command_frame(b"\x80\x00\x02\x00")
        assert result == client._response
        assert client._execute_call_count == 1

    def test_retrans_after_first_timeout(self):
        """首次丢包 → 重传成功 (第 2 次成功)"""
        driver = _make_driver(udp_retries=3)
        client = _FakeUdpFinsConnection()
        client.set_timeout_sequence(True, False)  # 第1次超时，第2次成功
        driver._wrap_udp_retransmission(client)

        result = client.execute_fins_command_frame(b"\x80\x00\x02\x00")
        assert result == client._response
        assert client._execute_call_count == 2

    def test_retrans_after_two_timeouts(self):
        """连续 2 次丢包 → 第 3 次成功"""
        driver = _make_driver(udp_retries=3)
        client = _FakeUdpFinsConnection()
        client.set_timeout_sequence(True, True, False)
        driver._wrap_udp_retransmission(client)

        result = client.execute_fins_command_frame(b"\x80\x00\x02\x00")
        assert result == client._response
        assert client._execute_call_count == 3

    def test_retrans_on_last_attempt(self):
        """前 3 次丢包 → 第 4 次 (最后一次) 成功"""
        driver = _make_driver(udp_retries=3)
        client = _FakeUdpFinsConnection()
        client.set_timeout_sequence(True, True, True, False)
        driver._wrap_udp_retransmission(client)

        result = client.execute_fins_command_frame(b"\x80\x00\x02\x00")
        assert result == client._response
        assert client._execute_call_count == 4  # 1 + 3 retries


class TestUdpRetransmissionExhausted:
    """UDP 重传耗尽 — 所有尝试均超时"""

    def test_all_retries_exhausted_raises_timeout(self):
        """4 次全超时 (1 + 3 retries) → 抛 socket.timeout"""
        driver = _make_driver(udp_retries=3)
        client = _FakeUdpFinsConnection()
        client.set_timeout_sequence(True, True, True, True)
        driver._wrap_udp_retransmission(client)

        with pytest.raises(socket.timeout):
            client.execute_fins_command_frame(b"\x80\x00\x02\x00")
        assert client._execute_call_count == 4  # 1 + 3 retries

    def test_exhausted_call_count_matches_retries(self):
        """重传次数 = _udp_max_retries + 1 (首次)"""
        driver = _make_driver(udp_retries=2)
        client = _FakeUdpFinsConnection()
        client.set_timeout_sequence(True, True, True)  # 3 次全超时
        driver._wrap_udp_retransmission(client)

        with pytest.raises(socket.timeout):
            client.execute_fins_command_frame(b"\x80\x00\x02\x00")
        assert client._execute_call_count == 3  # 1 + 2 retries

    def test_zero_retries_single_attempt(self):
        """udp_retries=0 → 仅 1 次尝试，不重传"""
        driver = _make_driver(udp_retries=0)
        client = _FakeUdpFinsConnection()
        client.set_timeout_sequence(True)  # 首次超时
        driver._wrap_udp_retransmission(client)

        with pytest.raises(socket.timeout):
            client.execute_fins_command_frame(b"\x80\x00\x02\x00")
        assert client._execute_call_count == 1


class TestUdpRetransmissionNoRetryOnOsError:
    """非 timeout 的 OSError 不重传 — 直接抛出"""

    def test_connection_reset_not_retried(self):
        """OSError (非 timeout) → 不重传，立即抛出"""
        driver = _make_driver(udp_retries=3)
        client = _FakeUdpFinsConnection()
        client._response = None  # 不走默认路径

        call_count = 0

        def raise_oserror(frame):
            nonlocal call_count
            call_count += 1
            raise OSError("connection reset")

        client.execute_fins_command_frame = raise_oserror
        driver._wrap_udp_retransmission(client)

        with pytest.raises(OSError, match="connection reset"):
            client.execute_fins_command_frame(b"\x80\x00\x02\x00")
        assert call_count == 1  # 不重传

    def test_connection_refused_not_retried(self):
        """ConnectionRefusedError → 不重传"""
        driver = _make_driver(udp_retries=3)
        client = _FakeUdpFinsConnection()
        client._response = None

        call_count = 0

        def raise_refused(frame):
            nonlocal call_count
            call_count += 1
            raise ConnectionRefusedError("refused")

        client.execute_fins_command_frame = raise_refused
        driver._wrap_udp_retransmission(client)

        with pytest.raises(ConnectionRefusedError):
            client.execute_fins_command_frame(b"\x80\x00\x02\x00")
        assert call_count == 1


class TestUdpRetransmissionDisabled:
    """udp_retries=0 禁用重传 — 保留原始行为"""

    def test_zero_retries_keeps_original_behavior_success(self):
        """udp_retries=0 + 成功 → 正常返回 (不包装)"""
        driver = _make_driver(udp_retries=0)
        client = _FakeUdpFinsConnection()
        original_func = client.execute_fins_command_frame.__func__
        driver._wrap_udp_retransmission(client)

        # udp_retries=0 时，方法不应被替换 (比较底层函数而非绑定方法对象)
        assert client.execute_fins_command_frame.__func__ is original_func

        result = client.execute_fins_command_frame(b"\x80\x00\x02\x00")
        assert result == client._response
        assert client._execute_call_count == 1


class TestUdpRetransmissionBackoff:
    """重传退避时间验证 — 指数退避 10ms, 20ms, 40ms (上限 200ms)"""

    def test_backoff_increases_exponentially(self):
        """退避时间: 10ms → 20ms → 40ms → 80ms → 160ms → 200ms (cap)"""
        driver = _make_driver(udp_retries=5)
        client = _FakeUdpFinsConnection()
        client.set_timeout_sequence(True, True, True, True, True, True)  # 6 次全超时

        sleep_calls: list[float] = []

        def mock_sleep(seconds):
            sleep_calls.append(seconds)

        driver._wrap_udp_retransmission(client)

        import edgelite.drivers.fins as fins_mod

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(fins_mod.time, "sleep", mock_sleep)
            with pytest.raises(socket.timeout):
                client.execute_fins_command_frame(b"\x80\x00\x02\x00")

        # 5 次重传 → 5 次 sleep (首次不 sleep)
        assert len(sleep_calls) == 5
        # 退避: 0.01, 0.02, 0.04, 0.08, 0.16
        assert sleep_calls[0] == pytest.approx(0.01)
        assert sleep_calls[1] == pytest.approx(0.02)
        assert sleep_calls[2] == pytest.approx(0.04)
        assert sleep_calls[3] == pytest.approx(0.08)
        assert sleep_calls[4] == pytest.approx(0.16)

    def test_backoff_capped_at_200ms(self):
        """退避上限 200ms — 第 5 次重传退避 = min(10*2^4, 200) = min(160, 200) = 160ms
        第 6 次重传退避 = min(10*2^5, 200) = min(320, 200) = 200ms"""
        driver = _make_driver(udp_retries=6)
        client = _FakeUdpFinsConnection()
        client.set_timeout_sequence(*([True] * 7))  # 7 次全超时

        sleep_calls: list[float] = []

        def mock_sleep(seconds):
            sleep_calls.append(seconds)

        driver._wrap_udp_retransmission(client)

        import edgelite.drivers.fins as fins_mod

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(fins_mod.time, "sleep", mock_sleep)
            with pytest.raises(socket.timeout):
                client.execute_fins_command_frame(b"\x80\x00\x02\x00")

        assert len(sleep_calls) == 6
        # 第 5 次 (index 4): 10*2^4 = 160ms
        assert sleep_calls[4] == pytest.approx(0.16)
        # 第 6 次 (index 5): min(10*2^5, 200) = 200ms
        assert sleep_calls[5] == pytest.approx(0.20)


# ══════════════════════════════════════════════════════════════════════
# 2. 配置与初始化
# ══════════════════════════════════════════════════════════════════════


class TestUdpRetransmissionConfig:
    """UDP 重传配置读取与默认值"""

    def test_config_schema_contains_udp_retries(self):
        """config_schema 包含 udp_retries 字段"""
        schema = OmronFinsDriver.config_schema
        fields = {f["name"]: f for f in schema["fields"]}
        assert "udp_retries" in fields
        assert fields["udp_retries"]["default"] == 3
        assert fields["udp_retries"]["min"] == 0
        assert fields["udp_retries"]["max"] == 10

    def test_default_udp_retries_constant(self):
        """_DEFAULT_UDP_RETRIES = 3"""
        assert OmronFinsDriver._DEFAULT_UDP_RETRIES == 3

    def test_config_schema_transport_field(self):
        """config_schema 包含 transport 字段 (tcp/udp)"""
        schema = OmronFinsDriver.config_schema
        fields = {f["name"]: f for f in schema["fields"]}
        assert "transport" in fields
        assert "udp" in fields["transport"]["options"]


# ══════════════════════════════════════════════════════════════════════
# 3. 包装器注入 — _do_connect / standby 路径
# ══════════════════════════════════════════════════════════════════════


class TestUdpRetransmissionInjection:
    """_wrap_udp_retransmission 正确替换 execute_fins_command_frame"""

    def test_method_replaced_after_wrap(self):
        """包装后 → execute_fins_command_frame 被替换为新函数"""
        driver = _make_driver(udp_retries=3)
        client = _FakeUdpFinsConnection()
        original = client.execute_fins_command_frame
        driver._wrap_udp_retransmission(client)
        assert client.execute_fins_command_frame is not original

    def test_original_method_preserved_in_closure(self):
        """包装后 → 原始方法仍被调用 (闭包保留引用)"""
        driver = _make_driver(udp_retries=3)
        client = _FakeUdpFinsConnection()
        driver._wrap_udp_retransmission(client)

        # 调用包装后的方法，验证原始逻辑仍执行
        result = client.execute_fins_command_frame(b"\x80\x00\x02\x00")
        assert result == client._response
        assert client._execute_call_count == 1

    def test_wrap_idempotent_safe(self):
        """多次包装 → 仍正常工作 (虽非推荐用法，但应不崩溃)"""
        driver = _make_driver(udp_retries=3)
        client = _FakeUdpFinsConnection()
        driver._wrap_udp_retransmission(client)
        # 第二次包装 — 会包装已包装的方法，但逻辑仍正确
        driver._wrap_udp_retransmission(client)

        result = client.execute_fins_command_frame(b"\x80\x00\x02\x00")
        assert result == client._response
