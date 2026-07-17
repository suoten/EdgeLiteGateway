"""消息分发器测试 - 路由/校验/设备管理/心跳握手

覆盖 engine/integration/dispatcher.py：
- MessageDispatcher: register_handler/register_service/dispatch
- dispatch 路由: 已注册 handler(sync/async)/heartbeat/handshake/unknown
- _validate_push_device_payload: device_id 正则/name/protocol/points/collect_interval
- _handle_push_device: 无服务/校验失败/创建成功/冲突重建
- _handle_delete_device: 无服务/缺 device_id/成功/失败
- _handle_device_control: 无服务/缺 device_id/无效 action
"""

from __future__ import annotations

import asyncio

import pytest

from edgelite.engine.integration.dispatcher import MessageDispatcher


class FakeDeviceService:
    """模拟 DeviceService"""

    def __init__(
        self,
        create_raises: Exception | None = None,
        create_raises_once: bool = False,
        delete_result: tuple[bool, str | None] = (True, None),
        get_device_result: dict | None = None,
    ):
        self._create_raises = create_raises
        self._create_raises_once = create_raises_once
        self._create_call_count = 0
        self._delete_result = delete_result
        self._get_device_result = get_device_result
        self.created: list[dict] = []
        self.deleted: list[str] = []

    async def create_device(self, payload: dict) -> None:
        self._create_call_count += 1
        if self._create_raises_once and self._create_call_count == 1:
            raise self._create_raises  # 仅第一次抛出（模拟冲突）
        if self._create_raises and not self._create_raises_once:
            raise self._create_raises
        self.created.append(payload)

    async def delete_device(self, device_id: str) -> tuple[bool, str | None]:
        self.deleted.append(device_id)
        return self._delete_result

    async def get_device(self, device_id: str) -> dict | None:
        return self._get_device_result

    async def remove_driver_instance(self, device_id: str):
        return None

    async def get_lifecycle(self):
        return None

    async def get_repo(self):
        return None


class TestMessageDispatcherRouting:
    @pytest.mark.asyncio
    async def test_dispatch_non_dict_payload(self):
        d = MessageDispatcher()
        result = await d.dispatch("test", "not a dict")  # type: ignore[arg-type]
        assert result["ok"] is False
        assert "JSON object" in result["error"]

    @pytest.mark.asyncio
    async def test_dispatch_unknown_type(self):
        d = MessageDispatcher()
        result = await d.dispatch("unknown_type", {})
        assert result["ok"] is False
        assert "Unknown message type" in result["error"]

    @pytest.mark.asyncio
    async def test_dispatch_heartbeat(self):
        d = MessageDispatcher()
        result = await d.dispatch("heartbeat", {})
        assert result["type"] == "heartbeat_ack"
        assert "timestamp" in result

    @pytest.mark.asyncio
    async def test_dispatch_handshake(self):
        d = MessageDispatcher()
        result = await d.dispatch("handshake", {})
        assert result["type"] == "handshake_ack"
        assert result["version"] == "1.0"

    @pytest.mark.asyncio
    async def test_dispatch_sync_handler(self):
        d = MessageDispatcher()
        d.register_handler("custom", lambda payload, sid: {"ok": True, "data": payload})
        result = await d.dispatch("custom", {"x": 1})
        assert result["ok"] is True
        assert result["data"] == {"x": 1}

    @pytest.mark.asyncio
    async def test_dispatch_async_handler(self):
        d = MessageDispatcher()

        async def handler(payload, sid):
            await asyncio.sleep(0)
            return {"ok": True, "async": True}

        d.register_handler("async_custom", handler)
        result = await d.dispatch("async_custom", {})
        assert result["ok"] is True
        assert result["async"] is True

    @pytest.mark.asyncio
    async def test_dispatch_handler_exception_returns_error(self):
        d = MessageDispatcher()
        d.register_handler("bad", lambda p, s: 1 / 0)
        result = await d.dispatch("bad", {})
        assert result["ok"] is False
        assert "error" in result


class TestValidatePushDevicePayload:
    def _make_valid_payload(self) -> dict:
        return {
            "device_id": "dev01",
            "name": "Temperature Sensor",
            "protocol": "modbus_tcp",
            "points": [{"name": "temp", "address": "40001"}],
            "collect_interval": 5,
        }

    def test_valid_payload(self):
        d = MessageDispatcher()
        assert d._validate_push_device_payload(self._make_valid_payload()) is None

    def test_missing_device_id(self):
        d = MessageDispatcher()
        payload = self._make_valid_payload()
        payload.pop("device_id")
        err = d._validate_push_device_payload(payload)
        assert err is not None
        assert "device_id" in err["error"]

    def test_invalid_device_id_uppercase(self):
        d = MessageDispatcher()
        payload = self._make_valid_payload()
        payload["device_id"] = "Dev01"  # 大写不合法
        err = d._validate_push_device_payload(payload)
        assert err is not None
        assert "device_id" in err["error"]

    def test_invalid_device_id_too_short(self):
        d = MessageDispatcher()
        payload = self._make_valid_payload()
        payload["device_id"] = "a"  # 太短（需至少2字符）
        err = d._validate_push_device_payload(payload)
        assert err is not None

    def test_invalid_device_id_underscore_at_start(self):
        d = MessageDispatcher()
        payload = self._make_valid_payload()
        payload["device_id"] = "_dev01"  # 以下划线开头不合法
        err = d._validate_push_device_payload(payload)
        assert err is not None

    def test_missing_name(self):
        d = MessageDispatcher()
        payload = self._make_valid_payload()
        payload.pop("name")
        err = d._validate_push_device_payload(payload)
        assert err is not None
        assert "name" in err["error"]

    def test_empty_name(self):
        d = MessageDispatcher()
        payload = self._make_valid_payload()
        payload["name"] = "   "
        err = d._validate_push_device_payload(payload)
        assert err is not None
        assert "name" in err["error"]

    def test_name_too_long(self):
        d = MessageDispatcher()
        payload = self._make_valid_payload()
        payload["name"] = "x" * 65
        err = d._validate_push_device_payload(payload)
        assert err is not None
        assert "too long" in err["error"]

    def test_missing_protocol(self):
        d = MessageDispatcher()
        payload = self._make_valid_payload()
        payload.pop("protocol")
        err = d._validate_push_device_payload(payload)
        assert err is not None
        assert "protocol" in err["error"]

    def test_missing_points(self):
        d = MessageDispatcher()
        payload = self._make_valid_payload()
        payload.pop("points")
        err = d._validate_push_device_payload(payload)
        assert err is not None
        assert "points" in err["error"]

    def test_empty_points_list(self):
        d = MessageDispatcher()
        payload = self._make_valid_payload()
        payload["points"] = []
        err = d._validate_push_device_payload(payload)
        assert err is not None
        assert "points" in err["error"]

    def test_points_not_list(self):
        d = MessageDispatcher()
        payload = self._make_valid_payload()
        payload["points"] = "not a list"
        err = d._validate_push_device_payload(payload)
        assert err is not None

    def test_collect_interval_below_minimum(self):
        d = MessageDispatcher()
        payload = self._make_valid_payload()
        payload["collect_interval"] = 0.5
        err = d._validate_push_device_payload(payload)
        assert err is not None
        assert "collect_interval" in err["error"]

    def test_collect_interval_not_number(self):
        d = MessageDispatcher()
        payload = self._make_valid_payload()
        payload["collect_interval"] = "fast"
        err = d._validate_push_device_payload(payload)
        assert err is not None


class TestHandlePushDevice:
    @pytest.mark.asyncio
    async def test_no_device_service(self):
        d = MessageDispatcher()
        result = await d.dispatch("push_device", {"device_id": "dev01"})
        assert result["ok"] is False
        assert "Device service not available" in result["error"]

    @pytest.mark.asyncio
    async def test_validation_error(self):
        d = MessageDispatcher()
        d.register_service("device_service", FakeDeviceService())
        result = await d.dispatch("push_device", {"device_id": ""})
        assert result["ok"] is False
        assert result.get("error_type") == "validation_error"

    @pytest.mark.asyncio
    async def test_create_success(self):
        svc = FakeDeviceService()
        d = MessageDispatcher()
        d.register_service("device_service", svc)
        payload = {
            "device_id": "dev01",
            "name": "Sensor",
            "protocol": "modbus_tcp",
            "points": [{"name": "temp"}],
            "collect_interval": 5,
        }
        result = await d.dispatch("push_device", payload)
        assert result["ok"] is True
        assert result["device_id"] == "dev01"
        assert len(svc.created) == 1

    @pytest.mark.asyncio
    async def test_conflict_triggers_rebuild(self):
        """create_device 抛出 already exists 时触发重建"""
        svc = FakeDeviceService(
            create_raises=ValueError("Device already exists"),
            create_raises_once=True,  # 仅第一次抛出，第二次（重建后）成功
            get_device_result=None,
            delete_result=(True, None),
        )
        d = MessageDispatcher()
        d.register_service("device_service", svc)
        payload = {
            "device_id": "dev01",
            "name": "Sensor",
            "protocol": "modbus_tcp",
            "points": [{"name": "temp"}],
            "collect_interval": 5,
        }
        result = await d.dispatch("push_device", payload)
        # 重建路径：先 delete 再 create
        assert result["ok"] is True
        assert result.get("rebuilt") is True
        assert "dev01" in svc.deleted

    @pytest.mark.asyncio
    async def test_create_failure_non_conflict(self):
        """非冲突的创建失败直接返回错误"""
        svc = FakeDeviceService(create_raises=RuntimeError("database locked"))
        d = MessageDispatcher()
        d.register_service("device_service", svc)
        payload = {
            "device_id": "dev01",
            "name": "Sensor",
            "protocol": "modbus_tcp",
            "points": [{"name": "temp"}],
            "collect_interval": 5,
        }
        result = await d.dispatch("push_device", payload)
        assert result["ok"] is False
        assert "database locked" in result["error"]


class TestHandleDeleteDevice:
    @pytest.mark.asyncio
    async def test_no_device_service(self):
        d = MessageDispatcher()
        result = await d.dispatch("delete_device", {"device_id": "dev01"})
        assert result["ok"] is False
        assert "Device service not available" in result["error"]

    @pytest.mark.asyncio
    async def test_missing_device_id(self):
        d = MessageDispatcher()
        d.register_service("device_service", FakeDeviceService())
        result = await d.dispatch("delete_device", {})
        assert result["ok"] is False
        assert result.get("error_type") == "validation_error"

    @pytest.mark.asyncio
    async def test_delete_success(self):
        svc = FakeDeviceService(delete_result=(True, None))
        d = MessageDispatcher()
        d.register_service("device_service", svc)
        result = await d.dispatch("delete_device", {"device_id": "dev01"})
        assert result["ok"] is True
        assert "dev01" in svc.deleted

    @pytest.mark.asyncio
    async def test_delete_failure(self):
        svc = FakeDeviceService(delete_result=(False, "not found"))
        d = MessageDispatcher()
        d.register_service("device_service", svc)
        result = await d.dispatch("delete_device", {"device_id": "dev01"})
        assert result["ok"] is False
        assert "not found" in result["error"]


class TestHandleDeviceControl:
    @pytest.mark.asyncio
    async def test_no_device_service(self):
        d = MessageDispatcher()
        result = await d.dispatch("device_control", {"device_id": "dev01"})
        assert result["ok"] is False
        assert "Device service not available" in result["error"]

    @pytest.mark.asyncio
    async def test_missing_device_id(self):
        d = MessageDispatcher()
        d.register_service("device_service", FakeDeviceService())
        result = await d.dispatch("device_control", {"action": "start_collect"})
        assert result["ok"] is False
        assert result.get("error_type") == "validation_error"

    @pytest.mark.asyncio
    async def test_invalid_action(self):
        d = MessageDispatcher()
        d.register_service("device_service", FakeDeviceService())
        result = await d.dispatch("device_control", {"device_id": "dev01", "action": "reboot"})
        assert result["ok"] is False
        assert "Invalid action" in result["error"]

    @pytest.mark.asyncio
    async def test_start_collect_no_scheduler(self):
        d = MessageDispatcher()
        d.register_service("device_service", FakeDeviceService())
        result = await d.dispatch("device_control", {"device_id": "dev01", "action": "start_collect"})
        assert result["ok"] is False
        assert "Scheduler not available" in result["error"]

    @pytest.mark.asyncio
    async def test_stop_collect_no_scheduler(self):
        d = MessageDispatcher()
        d.register_service("device_service", FakeDeviceService())
        result = await d.dispatch("device_control", {"device_id": "dev01", "action": "stop_collect"})
        assert result["ok"] is False
        assert "Scheduler not available" in result["error"]


class TestRegisterService:
    def test_register_and_retrieve(self):
        d = MessageDispatcher()
        svc = FakeDeviceService()
        d.register_service("device_service", svc)
        assert d._services["device_service"] is svc
