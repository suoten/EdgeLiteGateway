"""OPC DA Gateway 驱动测试

覆盖模块：src/edgelite/drivers/opc_da_gateway.py
- OpcDaSubscription 数据类
- OpcDaGatewayClient: connect/disconnect/list_servers/connect_server/
  browse/read/write/create_subscription/remove_subscription/on_data/is_connected
- OpcDaGatewayDriver: start/stop/add_device/read_points/write_point/
  discover_devices/browse_items/_ensure_connection/on_data/is_device_connected

所有 HTTP 调用通过 mock httpx.AsyncClient 替换，不发起真实网络请求。
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, "src")

from edgelite.drivers.opc_da_gateway import (
    OpcDaGatewayClient,
    OpcDaGatewayDriver,
    OpcDaSubscription,
)

# ── 辅助：构造 mock httpx 响应与客户端 ──


def _make_response(status_code=200, json_data=None):
    """构造 mock httpx.Response"""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json = MagicMock(return_value=json_data or {})
    return resp


def _make_httpx_client_mock(
    get_response=None,
    post_response=None,
    delete_response=None,
    get_side_effect=None,
    post_side_effect=None,
):
    """构造 mock httpx.AsyncClient 实例。

    支持 get/post/delete 返回指定响应，或通过 side_effect 抛异常。
    """
    client = AsyncMock()
    if get_side_effect is not None:
        client.get = AsyncMock(side_effect=get_side_effect)
    else:
        client.get = AsyncMock(return_value=get_response or _make_response(200, {}))

    if post_side_effect is not None:
        client.post = AsyncMock(side_effect=post_side_effect)
    else:
        client.post = AsyncMock(return_value=post_response or _make_response(200, {}))

    client.delete = AsyncMock(return_value=delete_response or _make_response(200, {}))
    client.aclose = AsyncMock()
    return client


def _make_httpx_module(client_mock=None, import_error=False):
    """构造 mock httpx 模块。

    Args:
        client_mock: 用作 AsyncClient 返回值的 mock 客户端
        import_error: 为 True 时模拟 httpx 未安装
    """
    mod = MagicMock()
    if import_error:
        mod.AsyncClient = MagicMock(side_effect=ImportError("no httpx"))
    else:
        mod.AsyncClient = MagicMock(return_value=client_mock or _make_httpx_client_mock())
    return mod


# ── OpcDaSubscription 数据类 ──


class TestOpcDaSubscription:
    def test_defaults(self):
        sub = OpcDaSubscription(group_name="g1", items=["tag1"])
        assert sub.group_name == "g1"
        assert sub.items == ["tag1"]
        assert sub.callback is None
        assert sub.update_rate == 1000

    def test_custom_values(self):
        def cb(x):
            return x

        sub = OpcDaSubscription(group_name="g2", items=["a", "b"], callback=cb, update_rate=500)
        assert sub.callback is cb
        assert sub.update_rate == 500
        assert len(sub.items) == 2


# ── OpcDaGatewayClient ──


class TestClientInit:
    def test_defaults(self):
        c = OpcDaGatewayClient()
        assert c._proxy_url == "http://localhost:8081"
        assert c._timeout == 10.0
        assert c._client is None
        assert c._connected is False
        assert c._subscriptions == {}
        assert c._latest_values == {}

    def test_strips_trailing_slash(self):
        c = OpcDaGatewayClient("http://host:8081/")
        assert c._proxy_url == "http://host:8081"

    def test_custom_url_and_timeout(self):
        c = OpcDaGatewayClient("http://192.168.1.1:9090", timeout=30.0)
        assert c._proxy_url == "http://192.168.1.1:9090"
        assert c._timeout == 30.0

    def test_is_connected_false_by_default(self):
        c = OpcDaGatewayClient()
        assert c.is_connected is False


class TestClientConnect:
    async def test_connect_success(self):
        client_mock = _make_httpx_client_mock(get_response=_make_response(200, {"status": "ok"}))
        c = OpcDaGatewayClient()
        with patch.dict(sys.modules, {"httpx": _make_httpx_module(client_mock=client_mock)}):
            result = await c.connect()
        assert result is True
        assert c._connected is True
        assert c.is_connected is True
        assert c._client is not None

    async def test_connect_health_check_failed(self):
        client_mock = _make_httpx_client_mock(get_response=_make_response(500, {}))
        c = OpcDaGatewayClient()
        with patch.dict(sys.modules, {"httpx": _make_httpx_module(client_mock=client_mock)}):
            result = await c.connect()
        assert result is False
        assert c._connected is False
        assert c.is_connected is False

    async def test_connect_exception_returns_false(self):
        client_mock = _make_httpx_client_mock(get_side_effect=RuntimeError("network error"))
        c = OpcDaGatewayClient()
        with patch.dict(sys.modules, {"httpx": _make_httpx_module(client_mock=client_mock)}):
            result = await c.connect()
        assert result is False
        assert c._connected is False

    async def test_connect_import_error(self):
        c = OpcDaGatewayClient()
        with patch.dict(sys.modules, {"httpx": None}):
            with pytest.raises(ImportError, match="httpx"):
                await c.connect()


class TestClientDisconnect:
    async def test_disconnect_closes_client(self):
        c = OpcDaGatewayClient()
        c._client = _make_httpx_client_mock()
        c._connected = True
        await c.disconnect()
        assert c._connected is False
        assert c._client is None
        c._client = None  # already set by disconnect

    async def test_disconnect_no_client_silent(self):
        c = OpcDaGatewayClient()
        await c.disconnect()
        assert c._connected is False
        assert c._client is None


class TestClientListServers:
    async def test_not_connected_returns_empty(self):
        c = OpcDaGatewayClient()
        assert await c.list_servers() == []

    async def test_list_servers_success(self):
        client_mock = _make_httpx_client_mock(get_response=_make_response(200, {"servers": ["ServerA", "ServerB"]}))
        c = OpcDaGatewayClient()
        c._client = client_mock
        c._connected = True
        result = await c.list_servers("myhost")
        assert result == ["ServerA", "ServerB"]
        client_mock.get.assert_awaited_once_with("/api/v1/servers?host=myhost")

    async def test_list_servers_non_200_returns_empty(self):
        client_mock = _make_httpx_client_mock(get_response=_make_response(404, {}))
        c = OpcDaGatewayClient()
        c._client = client_mock
        c._connected = True
        assert await c.list_servers() == []

    async def test_list_servers_exception_returns_empty(self):
        client_mock = _make_httpx_client_mock(get_side_effect=RuntimeError("err"))
        c = OpcDaGatewayClient()
        c._client = client_mock
        c._connected = True
        assert await c.list_servers() == []


class TestClientConnectServer:
    async def test_not_connected_returns_false(self):
        c = OpcDaGatewayClient()
        assert await c.connect_server("srv") is False

    async def test_connect_server_success_200(self):
        client_mock = _make_httpx_client_mock(post_response=_make_response(200, {}))
        c = OpcDaGatewayClient()
        c._client = client_mock
        c._connected = True
        assert await c.connect_server("ServerA") is True
        client_mock.post.assert_awaited_once_with("/api/v1/connect", json={"server": "ServerA"})

    async def test_connect_server_success_201(self):
        client_mock = _make_httpx_client_mock(post_response=_make_response(201, {}))
        c = OpcDaGatewayClient()
        c._client = client_mock
        c._connected = True
        assert await c.connect_server("ServerA") is True

    async def test_connect_server_failure_status(self):
        client_mock = _make_httpx_client_mock(post_response=_make_response(500, {}))
        c = OpcDaGatewayClient()
        c._client = client_mock
        c._connected = True
        assert await c.connect_server("ServerA") is False

    async def test_connect_server_exception_returns_false(self):
        client_mock = _make_httpx_client_mock(post_side_effect=RuntimeError("err"))
        c = OpcDaGatewayClient()
        c._client = client_mock
        c._connected = True
        assert await c.connect_server("ServerA") is False


class TestClientBrowse:
    async def test_not_connected_returns_empty(self):
        c = OpcDaGatewayClient()
        assert await c.browse() == []

    async def test_browse_success(self):
        items = [{"id": "tag1", "name": "Tag 1"}, {"id": "tag2", "name": "Tag 2"}]
        client_mock = _make_httpx_client_mock(get_response=_make_response(200, {"items": items}))
        c = OpcDaGatewayClient()
        c._client = client_mock
        c._connected = True
        result = await c.browse("root/path")
        assert result == items
        client_mock.get.assert_awaited_once_with("/api/v1/browse", params={"path": "root/path"})

    async def test_browse_non_200_returns_empty(self):
        client_mock = _make_httpx_client_mock(get_response=_make_response(404, {}))
        c = OpcDaGatewayClient()
        c._client = client_mock
        c._connected = True
        assert await c.browse() == []

    async def test_browse_exception_returns_empty(self):
        client_mock = _make_httpx_client_mock(get_side_effect=RuntimeError("err"))
        c = OpcDaGatewayClient()
        c._client = client_mock
        c._connected = True
        assert await c.browse() == []


class TestClientRead:
    async def test_not_connected_returns_none_dict(self):
        c = OpcDaGatewayClient()
        result = await c.read(["tag1", "tag2"])
        assert result == {"tag1": None, "tag2": None}

    async def test_read_success_good_quality(self):
        client_mock = _make_httpx_client_mock(
            post_response=_make_response(
                200,
                {
                    "results": [
                        {"id": "tag1", "value": 42, "quality": "good"},
                        {"id": "tag2", "value": "hello", "quality": "good"},
                    ]
                },
            )
        )
        c = OpcDaGatewayClient()
        c._client = client_mock
        c._connected = True
        result = await c.read(["tag1", "tag2"])
        assert result == {"tag1": 42, "tag2": "hello"}
        assert c._latest_values["tag1"] == 42
        assert c._latest_values["tag2"] == "hello"

    async def test_read_bad_quality_returns_none(self):
        client_mock = _make_httpx_client_mock(
            post_response=_make_response(
                200,
                {
                    "results": [
                        {"id": "tag1", "value": 42, "quality": "bad"},
                    ]
                },
            )
        )
        c = OpcDaGatewayClient()
        c._client = client_mock
        c._connected = True
        result = await c.read(["tag1"])
        assert result == {"tag1": None}
        # bad quality values should not be cached
        assert "tag1" not in c._latest_values

    async def test_read_mixed_quality(self):
        client_mock = _make_httpx_client_mock(
            post_response=_make_response(
                200,
                {
                    "results": [
                        {"id": "tag1", "value": 1, "quality": "good"},
                        {"id": "tag2", "value": 2, "quality": "bad"},
                        {"id": "tag3", "value": 3, "quality": "good"},
                    ]
                },
            )
        )
        c = OpcDaGatewayClient()
        c._client = client_mock
        c._connected = True
        result = await c.read(["tag1", "tag2", "tag3"])
        assert result == {"tag1": 1, "tag2": None, "tag3": 3}

    async def test_read_caches_good_values_only(self):
        client_mock = _make_httpx_client_mock(
            post_response=_make_response(
                200,
                {
                    "results": [
                        {"id": "tag1", "value": 100, "quality": "good"},
                        {"id": "tag2", "value": 200, "quality": "uncertain"},
                    ]
                },
            )
        )
        c = OpcDaGatewayClient()
        c._client = client_mock
        c._connected = True
        await c.read(["tag1", "tag2"])
        assert c._latest_values.get("tag1") == 100
        assert c._latest_values.get("tag2") is None

    async def test_read_non_200_fallback_to_cache(self):
        c = OpcDaGatewayClient()
        c._latest_values = {"tag1": 999}
        client_mock = _make_httpx_client_mock(post_response=_make_response(500, {}))
        c._client = client_mock
        c._connected = True
        result = await c.read(["tag1"])
        assert result == {"tag1": 999}

    async def test_read_exception_fallback_to_cache(self):
        c = OpcDaGatewayClient()
        c._latest_values = {"tag1": 888}
        client_mock = _make_httpx_client_mock(post_side_effect=RuntimeError("err"))
        c._client = client_mock
        c._connected = True
        result = await c.read(["tag1"])
        assert result == {"tag1": 888}

    async def test_read_exception_no_cache_returns_none(self):
        client_mock = _make_httpx_client_mock(post_side_effect=RuntimeError("err"))
        c = OpcDaGatewayClient()
        c._client = client_mock
        c._connected = True
        result = await c.read(["tag1"])
        assert result == {"tag1": None}


class TestClientWrite:
    async def test_not_connected_returns_false(self):
        c = OpcDaGatewayClient()
        assert await c.write("tag1", 42) is False

    async def test_write_success(self):
        client_mock = _make_httpx_client_mock(post_response=_make_response(200, {"success": True}))
        c = OpcDaGatewayClient()
        c._client = client_mock
        c._connected = True
        assert await c.write("tag1", 42) is True
        client_mock.post.assert_awaited_once_with("/api/v1/write", json={"item": "tag1", "value": 42})

    async def test_write_success_false_in_response(self):
        client_mock = _make_httpx_client_mock(post_response=_make_response(200, {"success": False}))
        c = OpcDaGatewayClient()
        c._client = client_mock
        c._connected = True
        assert await c.write("tag1", 42) is False

    async def test_write_non_200_returns_false(self):
        client_mock = _make_httpx_client_mock(post_response=_make_response(500, {}))
        c = OpcDaGatewayClient()
        c._client = client_mock
        c._connected = True
        assert await c.write("tag1", 42) is False

    async def test_write_exception_returns_false(self):
        client_mock = _make_httpx_client_mock(post_side_effect=RuntimeError("err"))
        c = OpcDaGatewayClient()
        c._client = client_mock
        c._connected = True
        assert await c.write("tag1", 42) is False


class TestClientCreateSubscription:
    async def test_not_connected_returns_none(self):
        c = OpcDaGatewayClient()
        assert await c.create_subscription("g1", ["tag1"]) is None

    async def test_create_success_200(self):
        client_mock = _make_httpx_client_mock(post_response=_make_response(200, {"subscription_id": "sub123"}))
        c = OpcDaGatewayClient()
        c._client = client_mock
        c._connected = True
        sub_id = await c.create_subscription("g1", ["tag1", "tag2"], update_rate=500)
        assert sub_id == "sub123"
        assert "sub123" in c._subscriptions
        assert c._subscriptions["sub123"].group_name == "g1"
        assert c._subscriptions["sub123"].items == ["tag1", "tag2"]
        assert c._subscriptions["sub123"].update_rate == 500

    async def test_create_success_201(self):
        client_mock = _make_httpx_client_mock(post_response=_make_response(201, {"subscription_id": "sub456"}))
        c = OpcDaGatewayClient()
        c._client = client_mock
        c._connected = True
        sub_id = await c.create_subscription("g1", ["tag1"])
        assert sub_id == "sub456"

    async def test_create_failure_status_returns_none(self):
        client_mock = _make_httpx_client_mock(post_response=_make_response(500, {}))
        c = OpcDaGatewayClient()
        c._client = client_mock
        c._connected = True
        assert await c.create_subscription("g1", ["tag1"]) is None

    async def test_create_exception_returns_none(self):
        client_mock = _make_httpx_client_mock(post_side_effect=RuntimeError("err"))
        c = OpcDaGatewayClient()
        c._client = client_mock
        c._connected = True
        assert await c.create_subscription("g1", ["tag1"]) is None

    async def test_create_default_update_rate(self):
        client_mock = _make_httpx_client_mock(post_response=_make_response(200, {"subscription_id": "s1"}))
        c = OpcDaGatewayClient()
        c._client = client_mock
        c._connected = True
        await c.create_subscription("g1", ["tag1"])
        assert c._subscriptions["s1"].update_rate == 1000


class TestClientRemoveSubscription:
    async def test_not_connected_returns_false(self):
        c = OpcDaGatewayClient()
        assert await c.remove_subscription("sub1") is False

    async def test_remove_success(self):
        client_mock = _make_httpx_client_mock(delete_response=_make_response(200, {}))
        c = OpcDaGatewayClient()
        c._client = client_mock
        c._connected = True
        c._subscriptions["sub1"] = OpcDaSubscription(group_name="g", items=[])
        assert await c.remove_subscription("sub1") is True
        assert "sub1" not in c._subscriptions
        client_mock.delete.assert_awaited_once_with("/api/v1/subscribe/sub1")

    async def test_remove_success_not_in_dict(self):
        """删除不在本地字典中的订阅也返回 True（HTTP 成功即可）"""
        client_mock = _make_httpx_client_mock(delete_response=_make_response(200, {}))
        c = OpcDaGatewayClient()
        c._client = client_mock
        c._connected = True
        assert await c.remove_subscription("unknown") is True

    async def test_remove_failure_status(self):
        client_mock = _make_httpx_client_mock(delete_response=_make_response(404, {}))
        c = OpcDaGatewayClient()
        c._client = client_mock
        c._connected = True
        assert await c.remove_subscription("sub1") is False

    async def test_remove_exception_returns_false(self):
        client_mock = _make_httpx_client_mock()
        client_mock.delete = AsyncMock(side_effect=RuntimeError("err"))
        c = OpcDaGatewayClient()
        c._client = client_mock
        c._connected = True
        assert await c.remove_subscription("sub1") is False


class TestClientOnData:
    def test_on_data_registers_callback(self):
        c = OpcDaGatewayClient()
        c._subscriptions["sub1"] = OpcDaSubscription(group_name="g", items=[])

        def cb(x):
            return x

        c.on_data("sub1", cb)
        assert c._subscriptions["sub1"].callback is cb

    def test_on_data_unknown_subscription_silent(self):
        c = OpcDaGatewayClient()
        c.on_data("unknown", lambda x: x)  # 不应抛异常


# ── OpcDaGatewayDriver ──


class TestDriverMetadata:
    def test_plugin_name(self):
        assert OpcDaGatewayDriver.plugin_name == "opc_da_gateway"

    def test_plugin_version(self):
        assert OpcDaGatewayDriver.plugin_version == "1.0.0"

    def test_supported_protocols(self):
        protos = OpcDaGatewayDriver.supported_protocols
        assert "opc_da_gateway" in protos
        assert "opc_da_proxy" in protos

    def test_config_schema_has_fields(self):
        schema = OpcDaGatewayDriver.config_schema
        field_names = {f["name"] for f in schema["fields"]}
        assert {"proxy_url", "timeout", "default_server", "default_host"} <= field_names

    def test_reconnect_constants(self):
        assert OpcDaGatewayDriver._MAX_RECONNECT_ATTEMPTS == 100
        assert OpcDaGatewayDriver._RECONNECT_BASE_DELAY == 1.0
        assert OpcDaGatewayDriver._RECONNECT_MAX_DELAY == 60.0


class TestDriverInit:
    def test_defaults(self):
        drv = OpcDaGatewayDriver()
        assert drv._running is False
        assert drv._client is None
        assert drv._config == {}
        assert drv._reconnect_count == 0
        assert drv._reconnect_delay == 1.0
        assert drv._connected is False
        assert drv._subscriptions == {}
        assert isinstance(drv._lock, asyncio.Lock)

    def test_is_device_connected_false_by_default(self):
        drv = OpcDaGatewayDriver()
        assert drv.is_device_connected("d1") is False


class TestDriverStart:
    async def test_start_success(self):
        drv = OpcDaGatewayDriver()
        client_mock = _make_httpx_client_mock(get_response=_make_response(200, {"status": "ok"}))
        with patch.dict(sys.modules, {"httpx": _make_httpx_module(client_mock=client_mock)}):
            await drv.start({"proxy_url": "http://h:8081", "timeout": 5})
        assert drv._running is True
        assert drv._connected is True
        assert drv._client is not None
        assert drv._reconnect_count == 0
        await drv.stop()

    async def test_start_with_default_server(self):
        drv = OpcDaGatewayDriver()
        client_mock = _make_httpx_client_mock(
            get_response=_make_response(200, {}),
            post_response=_make_response(200, {}),
        )
        with patch.dict(sys.modules, {"httpx": _make_httpx_module(client_mock=client_mock)}):
            await drv.start({"proxy_url": "http://h:8081", "default_server": "ServerA"})
        assert drv._running is True
        # Should have called connect_server via POST /api/v1/connect
        client_mock.post.assert_any_call("/api/v1/connect", json={"server": "ServerA"})
        await drv.stop()

    async def test_start_connect_failure_does_not_raise(self):
        """connect() 返回 False 时不抛异常，仅记录警告"""
        drv = OpcDaGatewayDriver()
        client_mock = _make_httpx_client_mock(get_response=_make_response(500, {}))
        with patch.dict(sys.modules, {"httpx": _make_httpx_module(client_mock=client_mock)}):
            await drv.start({"proxy_url": "http://h:8081"})
        assert drv._running is False
        assert drv._connected is False

    async def test_start_exception_raises(self):
        drv = OpcDaGatewayDriver()
        with patch.object(OpcDaGatewayClient, "connect", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError, match="boom"):
                await drv.start({"proxy_url": "http://h:8081"})

    async def test_start_defaults_proxy_url(self):
        drv = OpcDaGatewayDriver()
        client_mock = _make_httpx_client_mock(get_response=_make_response(200, {}))
        with patch.dict(sys.modules, {"httpx": _make_httpx_module(client_mock=client_mock)}):
            await drv.start({})
        assert drv._client is not None
        assert drv._client._proxy_url == "http://localhost:8081"
        await drv.stop()


class TestDriverStop:
    async def test_stop_cleans_resources(self):
        drv = OpcDaGatewayDriver()
        client_mock = _make_httpx_client_mock(delete_response=_make_response(200, {}))
        drv._client = client_mock
        drv._running = True
        drv._connected = True
        drv._subscriptions = {"d1": "sub1", "d2": "sub2"}
        await drv.stop()
        assert drv._running is False
        assert drv._connected is False
        assert drv._client is None
        # Should have removed both subscriptions
        assert client_mock.remove_subscription.await_count == 2
        client_mock.disconnect.assert_awaited_once()

    async def test_stop_no_client_silent(self):
        drv = OpcDaGatewayDriver()
        drv._running = True
        await drv.stop()
        assert drv._running is False
        assert drv._client is None

    async def test_stop_no_subscriptions(self):
        drv = OpcDaGatewayDriver()
        client_mock = _make_httpx_client_mock()
        drv._client = client_mock
        drv._running = True
        drv._connected = True
        await drv.stop()
        client_mock.disconnect.assert_awaited_once()
        assert drv._client is None


class TestDriverAddDevice:
    async def test_not_connected_returns(self):
        drv = OpcDaGatewayDriver()
        await drv.add_device("d1", {}, [{"address": "tag1"}])
        assert drv._subscriptions == {}

    async def test_add_device_creates_subscription(self):
        drv = OpcDaGatewayDriver()
        client_mock = AsyncMock()
        client_mock.create_subscription = AsyncMock(return_value="sub1")
        drv._client = client_mock
        drv._connected = True
        await drv.add_device("d1", {"update_rate": 500}, [{"address": "tag1"}, {"address": "tag2"}, {"name": "tag3"}])
        assert drv._subscriptions["d1"] == "sub1"
        client_mock.create_subscription.assert_awaited_once()
        call_kwargs = client_mock.create_subscription.call_args
        assert call_kwargs.kwargs["group_name"] == "edgelite_d1"
        assert set(call_kwargs.kwargs["items"]) == {"tag1", "tag2", "tag3"}
        assert call_kwargs.kwargs["update_rate"] == 500

    async def test_add_device_no_items_returns(self):
        drv = OpcDaGatewayDriver()
        client_mock = AsyncMock()
        client_mock.create_subscription = AsyncMock(return_value="sub1")
        drv._client = client_mock
        drv._connected = True
        await drv.add_device("d1", {}, [])
        assert drv._subscriptions == {}
        client_mock.create_subscription.assert_not_awaited()

    async def test_add_device_empty_addresses_filtered(self):
        """空地址的测点应被过滤"""
        drv = OpcDaGatewayDriver()
        client_mock = AsyncMock()
        client_mock.create_subscription = AsyncMock(return_value="sub1")
        drv._client = client_mock
        drv._connected = True
        await drv.add_device("d1", {}, [{"address": ""}, {"name": ""}])
        # All items filtered out, no subscription created
        assert drv._subscriptions == {}

    async def test_add_device_subscription_failure_no_store(self):
        """create_subscription 返回 None 时不存储"""
        drv = OpcDaGatewayDriver()
        client_mock = AsyncMock()
        client_mock.create_subscription = AsyncMock(return_value=None)
        drv._client = client_mock
        drv._connected = True
        await drv.add_device("d1", {}, [{"address": "tag1"}])
        assert "d1" not in drv._subscriptions

    async def test_add_device_uses_name_when_no_address(self):
        drv = OpcDaGatewayDriver()
        client_mock = AsyncMock()
        client_mock.create_subscription = AsyncMock(return_value="sub1")
        drv._client = client_mock
        drv._connected = True
        await drv.add_device("d1", {}, [{"name": "fallback_name"}])
        call_kwargs = client_mock.create_subscription.call_args.kwargs
        assert "fallback_name" in call_kwargs["items"]


class TestDriverReadPoints:
    async def test_not_running_triggers_ensure_connection(self):
        drv = OpcDaGatewayDriver()
        drv._config = {}
        result = await drv.read_points("d1", ["tag1"])
        assert result == {}

    async def test_read_points_success(self):
        drv = OpcDaGatewayDriver()
        client_mock = AsyncMock()
        client_mock.read = AsyncMock(return_value={"tag1": 42})
        drv._client = client_mock
        drv._running = True
        drv._connected = True
        result = await drv.read_points("d1", ["tag1"])
        assert result == {"tag1": 42}

    async def test_read_points_exception_returns_none_dict(self):
        drv = OpcDaGatewayDriver()
        client_mock = AsyncMock()
        client_mock.read = AsyncMock(side_effect=RuntimeError("read err"))
        drv._client = client_mock
        drv._running = True
        drv._connected = True
        result = await drv.read_points("d1", ["tag1", "tag2"])
        assert result == {"tag1": None, "tag2": None}


class TestDriverWritePoint:
    async def test_not_running_returns_false(self):
        drv = OpcDaGatewayDriver()
        drv._config = {}
        assert await drv.write_point("d1", "tag1", 42) is False

    async def test_write_success(self):
        drv = OpcDaGatewayDriver()
        client_mock = AsyncMock()
        client_mock.write = AsyncMock(return_value=True)
        drv._client = client_mock
        drv._running = True
        drv._connected = True
        assert await drv.write_point("d1", "tag1", 42) is True

    async def test_write_exception_returns_false(self):
        drv = OpcDaGatewayDriver()
        client_mock = AsyncMock()
        client_mock.write = AsyncMock(side_effect=RuntimeError("write err"))
        drv._client = client_mock
        drv._running = True
        drv._connected = True
        assert await drv.write_point("d1", "tag1", 42) is False


class TestDriverDiscoverDevices:
    async def test_not_connected_returns_empty(self):
        drv = OpcDaGatewayDriver()
        assert await drv.discover_devices({}) == []

    async def test_discover_success(self):
        drv = OpcDaGatewayDriver()
        client_mock = AsyncMock()
        client_mock.list_servers = AsyncMock(return_value=["SrvA", "SrvB"])
        drv._client = client_mock
        drv._connected = True
        drv._config = {"default_host": "myhost"}
        result = await drv.discover_devices({})
        assert len(result) == 2
        assert result[0]["name"] == "SrvA"
        assert result[0]["protocol"] == "opc_da_gateway"
        assert result[1]["server_id"] == "SrvB"

    async def test_discover_with_host_param(self):
        drv = OpcDaGatewayDriver()
        client_mock = AsyncMock()
        client_mock.list_servers = AsyncMock(return_value=["SrvA"])
        drv._client = client_mock
        drv._connected = True
        drv._config = {"default_host": "default_host"}
        result = await drv.discover_devices({"host": "custom_host"})
        client_mock.list_servers.assert_awaited_once_with("custom_host")
        assert len(result) == 1

    async def test_discover_exception_returns_empty(self):
        drv = OpcDaGatewayDriver()
        client_mock = AsyncMock()
        client_mock.list_servers = AsyncMock(side_effect=RuntimeError("discover err"))
        drv._client = client_mock
        drv._connected = True
        assert await drv.discover_devices({}) == []


class TestDriverBrowseItems:
    async def test_not_connected_returns_empty(self):
        drv = OpcDaGatewayDriver()
        assert await drv.browse_items() == []

    async def test_browse_success(self):
        drv = OpcDaGatewayDriver()
        items = [{"id": "tag1", "name": "Tag1"}]
        client_mock = AsyncMock()
        client_mock.browse = AsyncMock(return_value=items)
        drv._client = client_mock
        drv._connected = True
        result = await drv.browse_items("root")
        assert result == items

    async def test_browse_exception_returns_empty(self):
        drv = OpcDaGatewayDriver()
        client_mock = AsyncMock()
        client_mock.browse = AsyncMock(side_effect=RuntimeError("browse err"))
        drv._client = client_mock
        drv._connected = True
        assert await drv.browse_items() == []


class TestDriverEnsureConnection:
    async def test_no_config_returns(self):
        drv = OpcDaGatewayDriver()
        await drv._ensure_connection("d1")
        assert drv._reconnect_count == 0

    async def test_increments_reconnect_count(self):
        drv = OpcDaGatewayDriver()
        drv._config = {"proxy_url": "http://h:8081"}
        drv._client = MagicMock()
        drv._client.connect = AsyncMock(return_value=False)
        # Patch sleep to avoid real delay
        with patch("asyncio.sleep", AsyncMock()):
            await drv._ensure_connection("d1")
        assert drv._reconnect_count == 1

    async def test_reconnect_success(self):
        drv = OpcDaGatewayDriver()
        drv._config = {"proxy_url": "http://h:8081"}
        drv._client = MagicMock()
        drv._client.connect = AsyncMock(return_value=True)
        drv._client.connect_server = AsyncMock()
        with patch("asyncio.sleep", AsyncMock()):
            await drv._ensure_connection("d1")
        assert drv._connected is True
        assert drv._reconnect_count == 0
        assert drv._reconnect_delay == 1.0

    async def test_reconnect_success_with_default_server(self):
        drv = OpcDaGatewayDriver()
        drv._config = {"proxy_url": "http://h:8081", "default_server": "SrvA"}
        drv._client = MagicMock()
        drv._client.connect = AsyncMock(return_value=True)
        drv._client.connect_server = AsyncMock()
        with patch("asyncio.sleep", AsyncMock()):
            await drv._ensure_connection("d1")
        drv._client.connect_server.assert_awaited_once_with("SrvA")

    async def test_reconnect_failure(self):
        drv = OpcDaGatewayDriver()
        drv._config = {"proxy_url": "http://h:8081"}
        drv._client = MagicMock()
        drv._client.connect = AsyncMock(return_value=False)
        with patch("asyncio.sleep", AsyncMock()):
            await drv._ensure_connection("d1")
        assert drv._connected is False
        assert drv._reconnect_count == 1
        assert drv._reconnect_delay == 2.0  # doubled

    async def test_reconnect_exception_silent(self):
        drv = OpcDaGatewayDriver()
        drv._config = {"proxy_url": "http://h:8081"}
        drv._client = MagicMock()
        drv._client.connect = AsyncMock(side_effect=RuntimeError("conn err"))
        with patch("asyncio.sleep", AsyncMock()):
            await drv._ensure_connection("d1")  # 不应抛异常
        assert drv._connected is False

    async def test_max_reconnect_attempts_abandoned(self):
        drv = OpcDaGatewayDriver()
        drv._config = {"proxy_url": "http://h:8081"}
        drv._reconnect_count = 101  # 超过 _MAX_RECONNECT_ATTEMPTS
        drv._client = MagicMock()
        drv._client.connect = AsyncMock()
        await drv._ensure_connection("d1")
        # Should return without calling connect
        drv._client.connect.assert_not_awaited()

    async def test_no_client_in_ensure(self):
        """_client 为 None 时不应崩溃"""
        drv = OpcDaGatewayDriver()
        drv._config = {"proxy_url": "http://h:8081"}
        drv._client = None
        with patch("asyncio.sleep", AsyncMock()):
            await drv._ensure_connection("d1")  # 不应抛异常

    async def test_reconnect_delay_capped_at_max(self):
        drv = OpcDaGatewayDriver()
        drv._config = {"proxy_url": "http://h:8081"}
        drv._reconnect_delay = 120.0  # 超过 max
        drv._client = MagicMock()
        drv._client.connect = AsyncMock(return_value=False)
        with patch("asyncio.sleep", AsyncMock()) as sleep_mock:
            await drv._ensure_connection("d1")
        # sleep should be called with min(120, 60) = 60
        sleep_mock.assert_awaited_with(60.0)


class TestDriverOnData:
    def test_on_data_registers(self):
        drv = OpcDaGatewayDriver()

        def cb(x):
            return x

        drv.on_data(cb)
        assert drv._data_callback is cb


class TestDriverIsDeviceConnected:
    def test_false_when_not_connected(self):
        drv = OpcDaGatewayDriver()
        assert drv.is_device_connected("d1") is False

    def test_true_when_connected(self):
        drv = OpcDaGatewayDriver()
        drv._connected = True
        drv._client = MagicMock()
        assert drv.is_device_connected("d1") is True

    def test_false_when_no_client(self):
        drv = OpcDaGatewayDriver()
        drv._connected = True
        drv._client = None
        assert drv.is_device_connected("d1") is False


# ── 端到端：start + read + stop ──


class TestEndToEnd:
    async def test_start_read_stop(self):
        drv = OpcDaGatewayDriver()
        client_mock = _make_httpx_client_mock(
            get_response=_make_response(200, {"status": "ok"}),
            post_response=_make_response(200, {"results": [{"id": "tag1", "value": 99, "quality": "good"}]}),
        )
        with patch.dict(sys.modules, {"httpx": _make_httpx_module(client_mock=client_mock)}):
            await drv.start({"proxy_url": "http://h:8081"})
            result = await drv.read_points("d1", ["tag1"])
            assert result == {"tag1": 99}
            await drv.stop()
        assert drv._client is None

    async def test_start_write_stop(self):
        drv = OpcDaGatewayDriver()
        client_mock = _make_httpx_client_mock(
            get_response=_make_response(200, {}),
            post_response=_make_response(200, {"success": True}),
        )
        with patch.dict(sys.modules, {"httpx": _make_httpx_module(client_mock=client_mock)}):
            await drv.start({"proxy_url": "http://h:8081"})
            ok = await drv.write_point("d1", "tag1", 42)
            assert ok is True
            await drv.stop()
        assert drv._client is None
