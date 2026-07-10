"""ABB机器人驱动单元测试

覆盖 Task #10 P0 修复：机器人驱动安全停止机制

修复内容：
1. __init__ 调用 super().__init__()，修复 remove_device 崩溃
2. _connected 初始化为 False（原为 True，未连接时误报在线）
3. supported_protocols 改为 tuple（原为 list 可变默认值）
4. 新增 emergency_stop() — 设置安全停止标志 + 尝试 RWS 停止运动
5. 新增 stop_motion() — 通过 RWS API 停止机器人运动
6. 新增 get_safety_state() — 读取安全控制器状态
7. 新增 reset_safety_stop() — 操作员确认安全后重置标志
8. write_point 增加安全保护 — 安全停止激活时拒绝所有写入
9. stop() 方法在 stop_on_disconnect 时先发送运动停止命令
10. write_point 失败时尝试停止运动 (stop_on_disconnect)
"""

import asyncio
import sys

sys.path.insert(0, "src")

from edgelite.drivers.abb_robot import AbbRobotDriver


def _make_driver() -> AbbRobotDriver:
    """构造 AbbRobotDriver 实例 (绕过 __init__ 的 httpx 依赖)，仅设置测试所需属性。"""
    driver = AbbRobotDriver.__new__(AbbRobotDriver)
    # 模拟 super().__init__() 初始化的关键基类属性
    driver._health_stats = {}
    driver._offline_since = {}
    driver._conn_state_lock = __import__("threading").RLock()
    # 驱动自身属性
    driver._running = False
    driver._config = {}
    driver._client = None
    driver._lock = asyncio.Lock()
    driver._base_url = "http://192.168.1.100:80/rw"
    driver._connected = False
    driver._reconnect_count = 0
    driver._reconnect_delay = 1.0
    driver._devices = {}
    driver._safety_stop_active = False
    driver._enable_safety_guard = True
    driver._stop_on_disconnect = True
    driver._MOTION_STOP_TIMEOUT = 5.0
    return driver


class _FakeResponse:
    """模拟 httpx.Response。"""

    def __init__(self, status_code: int = 200, json_data: dict | list | None = None, text: str = ""):
        self.status_code = status_code
        self._json_data = json_data if json_data is not None else {}
        self.text = text or str(json_data)

    def json(self):
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


class _FakeAsyncClient:
    """模拟 httpx.AsyncClient，记录请求并返回预设响应。"""

    def __init__(self, responses: dict[str, _FakeResponse] | None = None):
        self._responses = responses or {}
        self.calls: list[tuple[str, str, dict]] = []  # (method, url, kwargs)

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self._responses.get(url, _FakeResponse(404))

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self._responses.get(url, _FakeResponse(404))

    async def put(self, url, **kwargs):
        self.calls.append(("PUT", url, kwargs))
        return self._responses.get(url, _FakeResponse(404))

    async def aclose(self):
        pass


class TestAbbRobotInit:
    """__init__ 初始化正确性测试"""

    def test_init_calls_super(self):
        """__init__ 调用 super().__init__()，基类属性可用"""
        driver = AbbRobotDriver()
        # _health_stats 和 _offline_since 由基类 __init__ 初始化
        assert hasattr(driver, "_health_stats"), "基类 _health_stats 未初始化 (super().__init__ 未调用)"
        assert hasattr(driver, "_offline_since"), "基类 _offline_since 未初始化"
        assert driver._health_stats == {}

    def test_connected_init_false(self):
        """_connected 初始化为 False (原 bug: 初始化为 True)"""
        driver = AbbRobotDriver()
        assert driver._connected is False

    def test_safety_stop_init_false(self):
        """_safety_stop_active 初始化为 False"""
        driver = AbbRobotDriver()
        assert driver._safety_stop_active is False
        assert driver.is_safety_stop_active is False

    def test_supported_protocols_is_tuple(self):
        """supported_protocols 是 tuple (原 bug: list 可变默认值)"""
        assert isinstance(AbbRobotDriver.supported_protocols, tuple)
        assert "abb_rws" in AbbRobotDriver.supported_protocols

    def test_remove_device_does_not_crash(self):
        """remove_device 不崩溃 (原 bug: _health_stats 未初始化抛 AttributeError)"""
        driver = AbbRobotDriver()
        # 不应抛出 AttributeError
        driver.remove_device("test_device_001")


class TestConfigSchema:
    """配置 schema 安全字段测试"""

    def test_safety_guard_field_exists(self):
        """enable_safety_guard 字段存在"""
        fields = AbbRobotDriver.config_schema["fields"]
        assert any(f["name"] == "enable_safety_guard" for f in fields)

    def test_stop_on_disconnect_field_exists(self):
        """stop_on_disconnect 字段存在"""
        fields = AbbRobotDriver.config_schema["fields"]
        assert any(f["name"] == "stop_on_disconnect" for f in fields)

    def test_safety_guard_default_true(self):
        """enable_safety_guard 默认为 True (生产环境推荐启用)"""
        fields = AbbRobotDriver.config_schema["fields"]
        guard = next(f for f in fields if f["name"] == "enable_safety_guard")
        assert guard["default"] is True

    def test_stop_on_disconnect_default_true(self):
        """stop_on_disconnect 默认为 True"""
        fields = AbbRobotDriver.config_schema["fields"]
        field = next(f for f in fields if f["name"] == "stop_on_disconnect")
        assert field["default"] is True


class TestEmergencyStop:
    """紧急停止机制测试"""

    def test_emergency_stop_sets_flag(self):
        """emergency_stop() 设置 _safety_stop_active = True"""
        driver = _make_driver()
        assert driver._safety_stop_active is False

        async def _run():
            return await driver.emergency_stop()

        result = asyncio.run(_run())
        assert result is True
        assert driver._safety_stop_active is True
        assert driver.is_safety_stop_active is True

    def test_emergency_stop_without_client_still_sets_flag(self):
        """emergency_stop() 在无 client 连接时仍设置标志 (不依赖 RWS)"""
        driver = _make_driver()
        driver._client = None
        driver._connected = False

        async def _run():
            return await driver.emergency_stop()

        result = asyncio.run(_run())
        assert result is True
        assert driver._safety_stop_active is True

    def test_emergency_stop_calls_stop_motion_when_connected(self):
        """emergency_stop() 在已连接时调用 stop_motion (通过 RWS 停止)"""
        driver = _make_driver()
        fake_client = _FakeAsyncClient(
            {
                "http://192.168.1.100:80/rw/motionctrl": _FakeResponse(202),
            }
        )
        driver._client = fake_client
        driver._connected = True

        async def _run():
            return await driver.emergency_stop()

        asyncio.run(_run())
        # 验证 stop_motion 被调用 (POST /rw/motionctrl)
        post_calls = [c for c in fake_client.calls if c[0] == "POST" and "motionctrl" in c[1]]
        assert len(post_calls) == 1

    def test_emergency_stop_does_not_raise_on_stop_motion_failure(self):
        """emergency_stop() 中 stop_motion 失败不传播异常"""
        driver = _make_driver()

        async def _failing_post(*args, **kwargs):
            raise ConnectionError("network down")

        class _BrokenClient:
            async def post(self, url, **kwargs):
                await _failing_post()

            async def get(self, url, **kwargs):
                pass

            async def aclose(self):
                pass

        driver._client = _BrokenClient()
        driver._connected = True

        async def _run():
            return await driver.emergency_stop()

        result = asyncio.run(_run())
        assert result is True
        assert driver._safety_stop_active is True


class TestWriteBlockOnSafetyStop:
    """安全停止激活时写入被阻止测试"""

    def test_write_blocked_when_safety_active(self):
        """安全停止激活时 write_point 返回 False"""
        driver = _make_driver()
        driver._safety_stop_active = True
        driver._running = True
        driver._connected = True
        driver._client = _FakeAsyncClient()

        async def _run():
            return await driver.write_point("dev1", "RAPID:T_ROB1:MainModule:target", 100)

        result = asyncio.run(_run())
        assert result is False

    def test_write_blocked_does_not_call_client(self):
        """安全停止激活时 write_point 不发送 HTTP 请求"""
        driver = _make_driver()
        driver._safety_stop_active = True
        driver._running = True
        driver._connected = True
        fake_client = _FakeAsyncClient()
        driver._client = fake_client

        async def _run():
            return await driver.write_point("dev1", "RAPID:T_ROB1:MainModule:x", 1)

        asyncio.run(_run())
        # 不应有任何 PUT 请求
        put_calls = [c for c in fake_client.calls if c[0] == "PUT"]
        assert len(put_calls) == 0

    def test_write_allowed_when_safety_inactive(self):
        """安全停止未激活时 write_point 正常执行"""
        driver = _make_driver()
        driver._safety_stop_active = False
        driver._running = True
        driver._connected = True
        fake_client = _FakeAsyncClient(
            {
                "http://192.168.1.100:80/rw/rapid/symbol/data": _FakeResponse(204),
            }
        )
        driver._client = fake_client

        async def _run():
            return await driver.write_point("dev1", "RAPID:T_ROB1:MainModule:x", 42)

        result = asyncio.run(_run())
        assert result is True

    def test_write_allowed_when_guard_disabled(self):
        """enable_safety_guard=False 时即使安全停止激活也允许写入"""
        driver = _make_driver()
        driver._safety_stop_active = True
        driver._enable_safety_guard = False  # 禁用安全保护
        driver._running = True
        driver._connected = True
        fake_client = _FakeAsyncClient(
            {
                "http://192.168.1.100:80/rw/rapid/symbol/data": _FakeResponse(204),
            }
        )
        driver._client = fake_client

        async def _run():
            return await driver.write_point("dev1", "RAPID:T_ROB1:MainModule:x", 42)

        result = asyncio.run(_run())
        assert result is True


class TestResetSafetyStop:
    """安全停止重置测试"""

    def test_reset_clears_flag(self):
        """reset_safety_stop() 清除安全停止标志"""
        driver = _make_driver()
        driver._safety_stop_active = True

        result = driver.reset_safety_stop()
        assert result is True
        assert driver._safety_stop_active is False
        assert driver.is_safety_stop_active is False

    def test_reset_returns_false_when_not_active(self):
        """安全停止未激活时 reset 返回 False"""
        driver = _make_driver()
        driver._safety_stop_active = False

        result = driver.reset_safety_stop()
        assert result is False

    def test_write_re_enabled_after_reset(self):
        """重置安全停止后 write_point 恢复正常"""
        driver = _make_driver()
        driver._safety_stop_active = True
        driver._running = True
        driver._connected = True
        fake_client = _FakeAsyncClient(
            {
                "http://192.168.1.100:80/rw/rapid/symbol/data": _FakeResponse(204),
            }
        )
        driver._client = fake_client

        # 重置前写入被阻止
        async def _write():
            return await driver.write_point("dev1", "RAPID:T_ROB1:M:x", 1)

        assert asyncio.run(_write()) is False

        # 重置
        driver.reset_safety_stop()

        # 重置后写入成功
        assert asyncio.run(_write()) is True


class TestStopMotion:
    """stop_motion() 方法测试"""

    def test_stop_motion_returns_true_on_success(self):
        """stop_motion() 成功返回 True"""
        driver = _make_driver()
        fake_client = _FakeAsyncClient(
            {
                "http://192.168.1.100:80/rw/motionctrl": _FakeResponse(202),
            }
        )
        driver._client = fake_client
        driver._connected = True

        async def _run():
            return await driver.stop_motion()

        result = asyncio.run(_run())
        assert result is True
        # 验证 POST 请求发送了 action=stop
        post_calls = [c for c in fake_client.calls if c[0] == "POST"]
        assert len(post_calls) == 1
        assert "stop" in str(post_calls[0][2].get("data", ""))

    def test_stop_motion_returns_false_when_not_connected(self):
        """未连接时 stop_motion() 返回 False"""
        driver = _make_driver()
        driver._client = None
        driver._connected = False

        async def _run():
            return await driver.stop_motion()

        result = asyncio.run(_run())
        assert result is False

    def test_stop_motion_returns_false_on_http_error(self):
        """HTTP 错误时 stop_motion() 返回 False"""
        driver = _make_driver()
        fake_client = _FakeAsyncClient(
            {
                "http://192.168.1.100:80/rw/motionctrl": _FakeResponse(500, text="Internal Error"),
            }
        )
        driver._client = fake_client
        driver._connected = True

        async def _run():
            return await driver.stop_motion()

        result = asyncio.run(_run())
        assert result is False

    def test_stop_motion_returns_false_on_exception(self):
        """网络异常时 stop_motion() 返回 False (不传播异常)"""
        driver = _make_driver()

        class _ErrorClient:
            async def post(self, url, **kwargs):
                raise ConnectionError("network unreachable")

            async def get(self, url, **kwargs):
                pass

            async def aclose(self):
                pass

        driver._client = _ErrorClient()
        driver._connected = True

        async def _run():
            return await driver.stop_motion()

        result = asyncio.run(_run())
        assert result is False


class TestGetSafetyState:
    """get_safety_state() 方法测试"""

    def test_returns_unavailable_when_not_connected(self):
        """未连接时返回 available=False"""
        driver = _make_driver()
        driver._client = None
        driver._connected = False

        async def _run():
            return await driver.get_safety_state()

        result = asyncio.run(_run())
        assert result["available"] is False

    def test_returns_state_on_success(self):
        """成功读取安全状态"""
        driver = _make_driver()
        fake_client = _FakeAsyncClient(
            {
                "http://192.168.1.100:80/rw/panel/safety_state": _FakeResponse(
                    200, {"payload": [{"safety_stop_state": "inactive"}]}
                ),
            }
        )
        driver._client = fake_client
        driver._connected = True

        async def _run():
            return await driver.get_safety_state()

        result = asyncio.run(_run())
        assert "payload" in result

    def test_syncs_safety_flag_from_controller(self):
        """控制器返回 active 状态时同步设置内部标志"""
        driver = _make_driver()
        driver._safety_stop_active = False
        fake_client = _FakeAsyncClient(
            {
                "http://192.168.1.100:80/rw/panel/safety_state": _FakeResponse(
                    200, {"payload": [{"safety_stop_state": "active"}]}
                ),
            }
        )
        driver._client = fake_client
        driver._connected = True

        async def _run():
            return await driver.get_safety_state()

        asyncio.run(_run())
        assert driver._safety_stop_active is True


class TestStopOnDisconnect:
    """stop() 方法的 stop_on_disconnect 行为测试"""

    def test_stop_calls_stop_motion_when_enabled(self):
        """stop() 在 stop_on_disconnect=True 时调用 stop_motion"""
        driver = _make_driver()
        fake_client = _FakeAsyncClient(
            {
                "http://192.168.1.100:80/rw/motionctrl": _FakeResponse(202),
            }
        )
        driver._client = fake_client
        driver._connected = True
        driver._running = True
        driver._stop_on_disconnect = True

        async def _run():
            await driver.stop()

        asyncio.run(_run())
        post_calls = [c for c in fake_client.calls if c[0] == "POST" and "motionctrl" in c[1]]
        assert len(post_calls) == 1

    def test_stop_skips_stop_motion_when_disabled(self):
        """stop() 在 stop_on_disconnect=False 时不调用 stop_motion"""
        driver = _make_driver()
        fake_client = _FakeAsyncClient()
        driver._client = fake_client
        driver._connected = True
        driver._running = True
        driver._stop_on_disconnect = False

        async def _run():
            await driver.stop()

        asyncio.run(_run())
        post_calls = [c for c in fake_client.calls if c[0] == "POST"]
        assert len(post_calls) == 0

    def test_stop_skips_stop_motion_when_not_connected(self):
        """stop() 在未连接时不尝试 stop_motion"""
        driver = _make_driver()
        driver._client = None
        driver._connected = False
        driver._running = True
        driver._stop_on_disconnect = True

        async def _run():
            await driver.stop()

        # 不应抛出异常
        asyncio.run(_run())
        assert driver._connected is False

    def test_stop_clears_connected_flag(self):
        """stop() 清除 _connected 标志"""
        driver = _make_driver()
        driver._client = None
        driver._connected = True
        driver._running = True

        async def _run():
            await driver.stop()

        asyncio.run(_run())
        assert driver._connected is False
        assert driver._running is False
