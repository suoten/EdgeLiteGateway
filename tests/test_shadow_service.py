"""设备影子服务测试

覆盖 services/shadow_service.py：
- ShadowService: start/stop 生命周期
- get_shadow / update_reported / update_desired / delete_shadow
- set_device_service / set_event_bus / set_audit_service 依赖注入
- update_desired 审计日志（正常 + 异常路径）
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, "src")

from edgelite.services.shadow_service import ShadowService


@pytest.fixture
def svc() -> ShadowService:
    return ShadowService()


class TestShadowServiceInit:
    def test_initial_state(self, svc: ShadowService):
        """新建实例应为空状态且未启动"""
        assert svc._started is False
        assert svc._shadows == {}
        assert svc._device_service is None
        assert svc._event_bus is None
        assert svc._audit_service is None

    def test_set_device_service(self, svc: ShadowService):
        dev = MagicMock()
        svc.set_device_service(dev)
        assert svc._device_service is dev

    def test_set_event_bus(self, svc: ShadowService):
        bus = MagicMock()
        svc.set_event_bus(bus)
        assert svc._event_bus is bus

    def test_set_audit_service(self, svc: ShadowService):
        audit = MagicMock()
        svc.set_audit_service(audit)
        assert svc._audit_service is audit


class TestShadowLifecycle:
    async def test_start_sets_started(self, svc: ShadowService):
        await svc.start()
        assert svc._started is True

    async def test_stop_clears_started(self, svc: ShadowService):
        await svc.start()
        assert svc._started is True
        await svc.stop()
        assert svc._started is False

    async def test_stop_without_start(self, svc: ShadowService):
        """stop 在未 start 时也应正常工作"""
        await svc.stop()
        assert svc._started is False


class TestGetShadow:
    def test_get_shadow_missing(self, svc: ShadowService):
        """不存在的设备返回 None"""
        assert svc.get_shadow("no-device") is None

    def test_get_shadow_after_reported(self, svc: ShadowService):
        svc.update_reported("dev1", {"temp": 42})
        shadow = svc.get_shadow("dev1")
        assert shadow is not None
        assert shadow["reported"] == {"temp": 42}
        assert shadow["desired"] == {}
        assert shadow["ts"] > 0

    def test_get_shadow_returns_reference(self, svc: ShadowService):
        """get_shadow 返回内部引用（修改会影响内部状态）"""
        svc.update_reported("dev1", {"a": 1})
        shadow = svc.get_shadow("dev1")
        assert shadow is not None
        shadow["reported"]["a"] = 99
        assert svc.get_shadow("dev1")["reported"]["a"] == 99


class TestUpdateReported:
    def test_update_reported_creates_shadow(self, svc: ShadowService):
        """首次上报创建影子结构"""
        svc.update_reported("dev1", {"temp": 25.5, "hum": 60})
        shadow = svc.get_shadow("dev1")
        assert shadow["reported"] == {"temp": 25.5, "hum": 60}
        assert shadow["desired"] == {}
        assert shadow["ts"] > 0

    def test_update_reported_merges(self, svc: ShadowService):
        """多次上报合并 reported"""
        svc.update_reported("dev1", {"temp": 25})
        svc.update_reported("dev1", {"hum": 60})
        shadow = svc.get_shadow("dev1")
        assert shadow["reported"] == {"temp": 25, "hum": 60}

    def test_update_reported_overwrites_same_key(self, svc: ShadowService):
        svc.update_reported("dev1", {"temp": 25})
        svc.update_reported("dev1", {"temp": 30})
        assert svc.get_shadow("dev1")["reported"] == {"temp": 30}

    def test_update_reported_updates_ts(self, svc: ShadowService):
        svc.update_reported("dev1", {"temp": 25})
        ts1 = svc.get_shadow("dev1")["ts"]
        import time as _time

        _time.sleep(0.001)
        svc.update_reported("dev1", {"temp": 26})
        ts2 = svc.get_shadow("dev1")["ts"]
        assert ts2 >= ts1

    def test_update_reported_empty_dict(self, svc: ShadowService):
        svc.update_reported("dev1", {})
        shadow = svc.get_shadow("dev1")
        assert shadow is not None
        assert shadow["reported"] == {}
        assert shadow["ts"] > 0

    def test_update_reported_preserves_desired(self, svc: ShadowService):
        svc.update_desired("dev1", {"setpoint": 50})
        svc.update_reported("dev1", {"temp": 25})
        shadow = svc.get_shadow("dev1")
        assert shadow["reported"] == {"temp": 25}
        assert shadow["desired"] == {"setpoint": 50}


class TestUpdateDesired:
    def test_update_desired_creates_shadow(self, svc: ShadowService):
        svc.update_desired("dev1", {"setpoint": 50})
        shadow = svc.get_shadow("dev1")
        assert shadow["desired"] == {"setpoint": 50}
        assert shadow["reported"] == {}
        assert shadow["ts"] > 0

    def test_update_desired_merges(self, svc: ShadowService):
        svc.update_desired("dev1", {"setpoint": 50})
        svc.update_desired("dev1", {"mode": "auto"})
        assert svc.get_shadow("dev1")["desired"] == {"setpoint": 50, "mode": "auto"}

    def test_update_desired_no_audit_service(self, svc: ShadowService):
        """无 audit_service 时不报错"""
        svc.update_desired("dev1", {"setpoint": 50})
        assert svc.get_shadow("dev1")["desired"] == {"setpoint": 50}

    def test_update_desired_with_audit_service(self, svc: ShadowService):
        """有 audit_service 时调用 log"""
        audit = MagicMock()
        svc.set_audit_service(audit)
        svc.update_desired("dev1", {"setpoint": 50})
        audit.log.assert_called_once_with(
            action="shadow_desired_update",
            target="dev1",
            detail={"setpoint": 50},
        )

    def test_update_desired_audit_exception_swallowed(self, svc: ShadowService):
        """audit_service.log 抛异常应被吞掉，不影响 desired 更新"""
        audit = MagicMock()
        audit.log.side_effect = RuntimeError("audit db down")
        svc.set_audit_service(audit)
        # 不应抛出
        svc.update_desired("dev1", {"setpoint": 50})
        assert svc.get_shadow("dev1")["desired"] == {"setpoint": 50}

    def test_update_desired_preserves_reported(self, svc: ShadowService):
        svc.update_reported("dev1", {"temp": 25})
        svc.update_desired("dev1", {"setpoint": 50})
        shadow = svc.get_shadow("dev1")
        assert shadow["reported"] == {"temp": 25}
        assert shadow["desired"] == {"setpoint": 50}


class TestDeleteShadow:
    def test_delete_existing_shadow(self, svc: ShadowService):
        svc.update_reported("dev1", {"temp": 25})
        assert svc.delete_shadow("dev1") is True
        assert svc.get_shadow("dev1") is None

    def test_delete_nonexistent_shadow(self, svc: ShadowService):
        """删除不存在的影子返回 False"""
        assert svc.delete_shadow("no-device") is False

    def test_delete_then_recreate(self, svc: ShadowService):
        svc.update_reported("dev1", {"temp": 25})
        svc.delete_shadow("dev1")
        svc.update_reported("dev1", {"temp": 30})
        shadow = svc.get_shadow("dev1")
        assert shadow is not None
        assert shadow["reported"] == {"temp": 30}
        assert shadow["desired"] == {}

    def test_delete_does_not_affect_others(self, svc: ShadowService):
        svc.update_reported("dev1", {"temp": 25})
        svc.update_reported("dev2", {"temp": 30})
        svc.delete_shadow("dev1")
        assert svc.get_shadow("dev1") is None
        assert svc.get_shadow("dev2") is not None


class TestShadowIsolation:
    def test_multiple_devices_independent(self, svc: ShadowService):
        svc.update_reported("dev1", {"temp": 25})
        svc.update_desired("dev2", {"setpoint": 50})
        s1 = svc.get_shadow("dev1")
        s2 = svc.get_shadow("dev2")
        assert s1["reported"] == {"temp": 25}
        assert s1["desired"] == {}
        assert s2["reported"] == {}
        assert s2["desired"] == {"setpoint": 50}
