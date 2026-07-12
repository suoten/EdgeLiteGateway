"""ONVIF 驱动单元测试

覆盖 src/edgelite/drivers/onvif_driver.py 的纯函数与静态方法：
- _classify_soap_error（SOAP 错误分类：认证/不支持/超时/通用）
- _is_auth_error（认证错误判断）
- _parse_ptz_status（PTZ 状态解析：PanTilt/Zoom/MoveStatus/UtcTime）
- OnvifDriver 类元数据

设计要点：
- 静态方法无需实例化，直接通过类调用
- _parse_ptz_status 使用 SimpleNamespace 模拟 ONVIF status 对象
- _classify_soap_error 验证关键字匹配逻辑
"""

from __future__ import annotations

from types import SimpleNamespace

from edgelite.drivers.onvif_driver import OnvifDriver

# ── _classify_soap_error ──


class TestClassifySoapError:
    def test_auth_error_notauthorized(self):
        assert OnvifDriver._classify_soap_error(Exception("ter:NotAuthorized")) == "ERR_ONVIF_AUTH_FAILED"

    def test_auth_error_access_denied(self):
        assert OnvifDriver._classify_soap_error(Exception("Access Denied")) == "ERR_ONVIF_AUTH_FAILED"

    def test_auth_error_401(self):
        assert OnvifDriver._classify_soap_error(Exception("HTTP 401 Unauthorized")) == "ERR_ONVIF_AUTH_FAILED"

    def test_not_supported_error(self):
        assert OnvifDriver._classify_soap_error(Exception("ter:ActionNotSupported")) == "ERR_ONVIF_NOT_SUPPORTED"

    def test_not_supported_unsupported(self):
        assert OnvifDriver._classify_soap_error(Exception("Operation unsupported")) == "ERR_ONVIF_NOT_SUPPORTED"

    def test_timeout_error(self):
        assert OnvifDriver._classify_soap_error(Exception("Connection timed out")) == "ERR_ONVIF_CONN_TIMEOUT"

    def test_connection_refused(self):
        assert OnvifDriver._classify_soap_error(Exception("Connection refused")) == "ERR_ONVIF_CONN_TIMEOUT"

    def test_generic_soap_error(self):
        assert OnvifDriver._classify_soap_error(Exception("Some unknown SOAP fault")) == "ERR_ONVIF_SOAP_ERROR"

    def test_empty_message(self):
        assert OnvifDriver._classify_soap_error(Exception("")) == "ERR_ONVIF_SOAP_ERROR"

    def test_case_insensitive(self):
        """错误消息大小写不敏感。"""
        assert OnvifDriver._classify_soap_error(Exception("NOTAUTHORIZED")) == "ERR_ONVIF_AUTH_FAILED"
        assert OnvifDriver._classify_soap_error(Exception("TIMEOUT")) == "ERR_ONVIF_CONN_TIMEOUT"


# ── _is_auth_error ──


class TestIsAuthError:
    def test_auth_error_returns_true(self):
        assert OnvifDriver._is_auth_error(Exception("ter:NotAuthorized")) is True

    def test_non_auth_error_returns_false(self):
        assert OnvifDriver._is_auth_error(Exception("Connection timeout")) is False

    def test_generic_error_returns_false(self):
        assert OnvifDriver._is_auth_error(Exception("Unknown error")) is False


# ── _parse_ptz_status ──


class TestParsePtzStatus:
    def test_none_returns_none(self):
        assert OnvifDriver._parse_ptz_status(None) is None

    def test_full_status_parsed(self):
        """完整 PTZ 状态：Position(PanTilt+Zoom) + MoveStatus + UtcTime。"""
        status = SimpleNamespace(
            Position=SimpleNamespace(
                PanTilt=SimpleNamespace(x=45.0, y=30.0),
                Zoom=SimpleNamespace(x=2.5),
            ),
            MoveStatus=SimpleNamespace(
                PanTilt="MOVING",
                Zoom="IDLE",
            ),
            UtcTime="2025-01-15T10:30:00Z",
        )
        result = OnvifDriver._parse_ptz_status(status)
        assert result is not None
        assert result["pan"] == 45.0
        assert result["tilt"] == 30.0
        assert result["zoom"] == 2.5
        assert result["pan_tilt_moving"] == "MOVING"
        assert result["zoom_moving"] == "IDLE"
        assert result["utc_time"] == "2025-01-15T10:30:00Z"

    def test_partial_status_only_position(self):
        status = SimpleNamespace(
            Position=SimpleNamespace(
                PanTilt=SimpleNamespace(x=10.0, y=20.0),
            ),
        )
        result = OnvifDriver._parse_ptz_status(status)
        assert result is not None
        assert result["pan"] == 10.0
        assert result["tilt"] == 20.0
        assert "zoom" not in result

    def test_partial_status_no_position(self):
        status = SimpleNamespace(
            MoveStatus=SimpleNamespace(Zoom="MOVING"),
        )
        result = OnvifDriver._parse_ptz_status(status)
        assert result is not None
        assert result["zoom_moving"] == "MOVING"

    def test_empty_status_returns_none(self):
        """无任何属性 → 返回 None。"""
        status = SimpleNamespace()
        result = OnvifDriver._parse_ptz_status(status)
        assert result is None

    def test_parse_exception_returns_none(self):
        """解析异常时返回 None（不抛出）。"""

        class BadAttr:
            @property
            def Position(self):  # noqa: N802 - ONVIF 规范属性名，必须大写
                raise RuntimeError("broken")

        result = OnvifDriver._parse_ptz_status(BadAttr())
        assert result is None


# ── OnvifDriver 类元数据 ──


class TestOnvifDriverMetadata:
    def test_plugin_name(self):
        assert OnvifDriver.plugin_name == "onvif"

    def test_supported_protocols(self):
        assert isinstance(OnvifDriver.supported_protocols, (tuple, list))

    def test_config_schema_exists(self):
        assert hasattr(OnvifDriver, "config_schema")

    def test_max_snapshot_size(self):
        """FIXED-P2: 快照最大 10MB，防止 OOM。"""
        assert OnvifDriver._MAX_SNAPSHOT_SIZE == 10 * 1024 * 1024
