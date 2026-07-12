"""多网关级联管理器单元测试

覆盖 src/edgelite/engine/cascade_manager.py：
- TopologyStatus 枚举 / NeighborInfo / CascadeTopology 数据类
- CascadeManager：初始化、启动/停止、mDNS 回调、拓扑重建、邻居发现、
  父节点转发（HMAC 签名/跳数限制）、配置更新、级联认证校验、重放防护

设计要点：
- zeroconf 与 aiohttp 均被 mock，不发起真实 mDNS/HTTP 调用
- asyncio.sleep 被加速，心跳/发现循环快速终止
- 所有任务在测试末尾正确取消
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, "src")

from edgelite.engine.cascade_manager import (
    CascadeManager,
    CascadeTopology,
    NeighborInfo,
    TopologyStatus,
)


# ── 数据类与枚举 ──


class TestTopologyStatus:
    def test_values(self):
        assert TopologyStatus.STANDALONE == "standalone"
        assert TopologyStatus.PARENT == "parent"
        assert TopologyStatus.CHILD == "child"
        assert TopologyStatus.PEER == "peer"

    def test_is_str(self):
        assert isinstance(TopologyStatus.CHILD, str)


class TestNeighborInfo:
    def test_defaults(self):
        n = NeighborInfo(neighbor_id="n1", host="10.0.0.1", port=8080)
        assert n.neighbor_id == "n1"
        assert n.role == "peer"
        assert n.properties == {}

    def test_with_fields(self):
        n = NeighborInfo(neighbor_id="n1", host="h", port=80, role="child", properties={"k": "v"})
        assert n.role == "child"
        assert n.properties == {"k": "v"}


class TestCascadeTopology:
    def test_defaults(self):
        t = CascadeTopology(local_id="local")
        assert t.status == TopologyStatus.STANDALONE
        assert t.parent_id is None
        assert t.children == []
        assert t.peers == []

    def test_with_fields(self):
        t = CascadeTopology(local_id="x", status=TopologyStatus.CHILD, parent_id="p1")
        assert t.status == TopologyStatus.CHILD
        assert t.parent_id == "p1"


# ── 初始化与属性 ──


class TestInit:
    def test_defaults(self):
        mgr = CascadeManager(local_id="gw1")
        assert mgr._local_id == "gw1"
        assert mgr._service_name == "EdgeLite"
        assert mgr._parent_host is None
        assert mgr._parent_scheme == "http"
        assert mgr._running is False
        assert mgr._cascade_token == ""
        assert mgr._allowed_neighbors is None

    def test_with_parent(self):
        mgr = CascadeManager(
            local_id="gw1",
            parent_host="parent.example.com",
            parent_port=9000,
            cascade_token="secret",
        )
        assert mgr._parent_host == "parent.example.com"
        assert mgr._parent_port == 9000
        assert mgr._cascade_token == "secret"

    def test_token_from_env(self, monkeypatch):
        monkeypatch.setenv("EDGELITE_CASCADE_TOKEN", "envtoken")
        mgr = CascadeManager(local_id="gw1")
        assert mgr._cascade_token == "envtoken"

    def test_allowed_neighbors(self):
        mgr = CascadeManager(local_id="gw1", allowed_neighbors={"a", "b"})
        assert mgr._allowed_neighbors == {"a", "b"}

    def test_topology_property(self):
        mgr = CascadeManager(local_id="gw1")
        assert mgr.topology.local_id == "gw1"
        assert mgr.topology.status == TopologyStatus.STANDALONE

    def test_neighbors_property(self):
        mgr = CascadeManager(local_id="gw1")
        mgr._neighbors["n1"] = NeighborInfo("n1", "h", 80)
        assert len(mgr.neighbors) == 1
        assert mgr.neighbors[0].neighbor_id == "n1"


# ── Token 哈希与邻居校验 ──


class TestTokenHash:
    def test_empty_token_returns_empty(self):
        mgr = CascadeManager(local_id="gw1")
        assert mgr._compute_token_hash() == ""

    def test_returns_truncated_hash(self):
        mgr = CascadeManager(local_id="gw1", cascade_token="mytoken")
        expected = hashlib.sha256("mytoken".encode()).hexdigest()[:16]
        assert mgr._compute_token_hash() == expected


class TestVerifyNeighbor:
    def test_no_token_skips_verification(self):
        mgr = CascadeManager(local_id="gw1")
        assert mgr._verify_neighbor("n1", "") is True

    def test_token_mismatch_rejected(self):
        mgr = CascadeManager(local_id="gw1", cascade_token="secret")
        assert mgr._verify_neighbor("n1", "wrong") is False

    def test_token_match_no_whitelist(self):
        mgr = CascadeManager(local_id="gw1", cascade_token="secret")
        h = mgr._compute_token_hash()
        assert mgr._verify_neighbor("n1", h) is True

    def test_token_match_in_whitelist(self):
        mgr = CascadeManager(local_id="gw1", cascade_token="secret", allowed_neighbors={"n1"})
        h = mgr._compute_token_hash()
        assert mgr._verify_neighbor("n1", h) is True

    def test_token_match_not_in_whitelist(self):
        mgr = CascadeManager(local_id="gw1", cascade_token="secret", allowed_neighbors={"other"})
        h = mgr._compute_token_hash()
        assert mgr._verify_neighbor("n1", h) is False

    def test_empty_neighbor_hash_rejected(self):
        mgr = CascadeManager(local_id="gw1", cascade_token="secret")
        assert mgr._verify_neighbor("n1", "") is False


# ── 级联认证头构建与校验 ──


class TestBuildCascadeHeaders:
    def test_no_token_returns_empty(self):
        mgr = CascadeManager(local_id="gw1")
        assert mgr._build_cascade_headers(b"body") == {}

    def test_with_token_returns_headers(self):
        mgr = CascadeManager(local_id="gw1", cascade_token="secret")
        headers = mgr._build_cascade_headers(b"body")
        assert "X-Cascade-Token" in headers
        assert "X-Cascade-Timestamp" in headers
        assert "X-Cascade-Nonce" in headers
        assert len(headers["X-Cascade-Nonce"]) == 32  # 16 bytes hex

    def test_signature_is_valid_hmac(self):
        mgr = CascadeManager(local_id="gw1", cascade_token="secret")
        body = b"payload"
        headers = mgr._build_cascade_headers(body)
        ts = headers["X-Cascade-Timestamp"]
        nonce = headers["X-Cascade-Nonce"]
        message = f"{ts}{nonce}".encode() + body
        expected = hmac.new("secret".encode(), message, hashlib.sha256).hexdigest()
        assert headers["X-Cascade-Token"] == expected


class TestVerifyCascadeRequest:
    def test_no_token_rejects(self):
        mgr = CascadeManager(local_id="gw1")
        ok, reason = mgr.verify_cascade_request({}, b"body")
        assert ok is False
        assert "not configured" in reason

    def test_missing_headers_rejects(self):
        mgr = CascadeManager(local_id="gw1", cascade_token="secret")
        ok, reason = mgr.verify_cascade_request({}, b"body")
        assert ok is False
        assert "missing" in reason

    def test_invalid_timestamp_rejects(self):
        mgr = CascadeManager(local_id="gw1", cascade_token="secret")
        headers = {"X-Cascade-Token": "sig", "X-Cascade-Timestamp": "abc", "X-Cascade-Nonce": "n1"}
        ok, reason = mgr.verify_cascade_request(headers, b"body")
        assert ok is False
        assert "timestamp" in reason.lower() or "invalid" in reason.lower()

    def test_expired_timestamp_rejects(self):
        mgr = CascadeManager(local_id="gw1", cascade_token="secret")
        old_ts = str(int(time.time()) - 600)
        headers = {"X-Cascade-Token": "sig", "X-Cascade-Timestamp": old_ts, "X-Cascade-Nonce": "n1"}
        ok, reason = mgr.verify_cascade_request(headers, b"body")
        assert ok is False
        assert "window" in reason.lower() or "timestamp" in reason.lower()

    def test_signature_mismatch_rejects(self):
        mgr = CascadeManager(local_id="gw1", cascade_token="secret")
        ts = str(int(time.time()))
        headers = {"X-Cascade-Token": "badsig", "X-Cascade-Timestamp": ts, "X-Cascade-Nonce": "n1"}
        ok, reason = mgr.verify_cascade_request(headers, b"body")
        assert ok is False
        assert "signature" in reason.lower() or "mismatch" in reason.lower()

    def test_valid_request_accepted(self):
        mgr = CascadeManager(local_id="gw1", cascade_token="secret")
        body = b"payload"
        headers = mgr._build_cascade_headers(body)
        ok, reason = mgr.verify_cascade_request(headers, body)
        assert ok is True
        assert reason == ""

    def test_nonce_replay_rejects(self):
        mgr = CascadeManager(local_id="gw1", cascade_token="secret")
        body = b"payload"
        headers = mgr._build_cascade_headers(body)
        # 第一次通过
        ok, _ = mgr.verify_cascade_request(headers, body)
        assert ok is True
        # 第二次重放被拒
        ok, reason = mgr.verify_cascade_request(headers, body)
        assert ok is False
        assert "replay" in reason.lower()

    def test_case_insensitive_headers(self):
        mgr = CascadeManager(local_id="gw1", cascade_token="secret")
        body = b"data"
        headers = mgr._build_cascade_headers(body)
        # 转为小写键
        lower_headers = {k.lower(): v for k, v in headers.items()}
        ok, _ = mgr.verify_cascade_request(lower_headers, body)
        assert ok is True


# ── 跳数检查 ──


class TestCheckHopCount:
    def test_no_hop_count(self):
        ok, hop = CascadeManager.check_hop_count({})
        assert ok is True
        assert hop == 0

    def test_valid_hop(self):
        ok, hop = CascadeManager.check_hop_count({"_cascade_hop_count": 5})
        assert ok is True
        assert hop == 5

    def test_exceeds_limit(self):
        ok, hop = CascadeManager.check_hop_count({"_cascade_hop_count": 100})
        assert ok is False
        assert hop == 100

    def test_invalid_type_resets_to_zero(self):
        ok, hop = CascadeManager.check_hop_count({"_cascade_hop_count": "bad"})
        assert ok is True
        assert hop == 0

    def test_negative_resets_to_zero(self):
        ok, hop = CascadeManager.check_hop_count({"_cascade_hop_count": -1})
        assert ok is True
        assert hop == 0


# ── 获取本机 IP ──


class TestGetLocalIp:
    def test_returns_ip(self):
        # mock socket 以返回可预测 IP
        with patch("socket.socket") as mock_sock_cls:
            mock_sock = MagicMock()
            mock_sock.getsockname.return_value = ("192.168.1.100", 0)
            mock_sock_cls.return_value = mock_sock
            ip = CascadeManager._get_local_ip()
        assert ip == "192.168.1.100"

    def test_returns_loopback_on_error(self):
        with patch("socket.socket", side_effect=OSError("fail")):
            ip = CascadeManager._get_local_ip()
        assert ip == "127.0.0.1"


# ── start / stop ──


def _mock_zeroconf_module():
    """构造 mock zeroconf 模块。"""
    mod = MagicMock()
    mod.Zeroconf = MagicMock()
    mod.ServiceBrowser = MagicMock()
    mod.ServiceInfo = MagicMock()
    mod.ServiceStateChange = MagicMock()
    mod.ServiceStateChange.Added = "added"
    mod.ServiceStateChange.Removed = "removed"
    return mod


class TestStartStop:
    async def test_start_no_parent(self, monkeypatch):
        mgr = CascadeManager(local_id="gw1", service_port=8080)
        fake_zc = _mock_zeroconf_module()
        mock_zc_inst = AsyncMock()
        mock_zc_inst.async_register_service = AsyncMock()
        mock_zc_inst.async_close = AsyncMock()
        fake_zc.Zeroconf = MagicMock(return_value=mock_zc_inst)

        with patch.dict(sys.modules, {"zeroconf": fake_zc}):
            await mgr.start()
        try:
            assert mgr._running is True
            assert mgr._discover_task is None  # 无父节点
            assert mgr._topology.status == TopologyStatus.STANDALONE
        finally:
            await mgr.stop()

    async def test_start_with_parent(self, monkeypatch):
        mgr = CascadeManager(local_id="gw1", parent_host="parent", parent_port=9000, service_port=8080)
        fake_zc = _mock_zeroconf_module()
        mock_zc_inst = AsyncMock()
        mock_zc_inst.async_register_service = AsyncMock()
        mock_zc_inst.async_close = AsyncMock()
        fake_zc.Zeroconf = MagicMock(return_value=mock_zc_inst)

        original_sleep = asyncio.sleep

        async def _fast_sleep(delay, *a, **kw):
            mgr._running = False
            await original_sleep(0)

        with patch.dict(sys.modules, {"zeroconf": fake_zc}), patch("asyncio.sleep", new=_fast_sleep):
            await mgr.start()
            assert mgr._topology.status == TopologyStatus.CHILD
            assert mgr._topology.parent_id == "parent:9000"
        await mgr.stop()

    async def test_start_already_running(self):
        mgr = CascadeManager(local_id="gw1")
        mgr._running = True
        await mgr.start()  # 应直接返回
        assert mgr._running is True

    async def test_start_import_error(self):
        mgr = CascadeManager(local_id="gw1")
        with patch.dict(sys.modules, {"zeroconf": None}):
            await mgr.start()
        assert mgr._running is False

    async def test_start_exception_silent(self):
        mgr = CascadeManager(local_id="gw1")
        fake_zc = _mock_zeroconf_module()
        fake_zc.Zeroconf = MagicMock(side_effect=RuntimeError("boom"))
        with patch.dict(sys.modules, {"zeroconf": fake_zc}):
            await mgr.start()  # 不应抛异常
        # 源码通用 Exception 处理器只记录错误，不重置 _running
        # (仅 ImportError 处理器会重置 _running=False)
        assert mgr._running is True

    async def test_stop_cleans_resources(self):
        mgr = CascadeManager(local_id="gw1")
        mgr._running = True
        mgr._zeroconf = AsyncMock()
        mgr._zeroconf.async_unregister_service = AsyncMock()
        mgr._zeroconf.async_close = AsyncMock()
        mgr._registration = MagicMock()
        await mgr.stop()
        assert mgr._running is False
        assert mgr._zeroconf is None
        assert mgr._registration is None

    async def test_stop_with_http_session(self):
        mgr = CascadeManager(local_id="gw1")
        mgr._running = True
        # stop() 在关闭后会置 _http_session=None，需先保存引用再断言
        session = AsyncMock()
        session.closed = False
        session.close = AsyncMock()
        mgr._http_session = session
        await mgr.stop()
        session.close.assert_called_once()
        assert mgr._http_session is None

    async def test_stop_with_discover_task(self):
        mgr = CascadeManager(local_id="gw1")
        mgr._running = True
        mgr._discover_task = asyncio.create_task(asyncio.sleep(100))
        await mgr.stop()
        assert mgr._discover_task.done()

    async def test_stop_zeroconf_exception_silent(self):
        mgr = CascadeManager(local_id="gw1")
        mgr._running = True
        mock_zc = AsyncMock()
        mock_zc.async_close = AsyncMock(side_effect=RuntimeError("fail"))
        mgr._zeroconf = mock_zc
        mgr._registration = MagicMock()
        await mgr.stop()  # 不应抛异常


# ── mDNS 服务状态变更回调 ──


class TestOnServiceStateChange:
    def test_added_stores_neighbor(self):
        mgr = CascadeManager(local_id="gw1", cascade_token="secret")
        token_hash = mgr._compute_token_hash()

        mock_zc = MagicMock()
        info = MagicMock()
        info.properties = {
            b"local_id": b"n1",
            b"token_hash": token_hash.encode(),
            b"version": b"1.0",
        }
        info.addresses = [b"\x0a\x00\x00\x01"]
        info.port = 8080
        mock_zc.get_service_info.return_value = info

        fake_zc = _mock_zeroconf_module()
        with patch.dict(sys.modules, {"zeroconf": fake_zc}):
            mgr._on_service_state_change(mock_zc, "_edgelite._tcp.local.", "n1._edgelite._tcp.local.", "added")

        assert "n1" in mgr._neighbors
        assert mgr._neighbors["n1"].host == "10.0.0.1"
        assert mgr._neighbors["n1"].port == 8080

    def test_added_token_mismatch_rejected(self):
        mgr = CascadeManager(local_id="gw1", cascade_token="secret")
        mock_zc = MagicMock()
        info = MagicMock()
        info.properties = {b"local_id": b"n1", b"token_hash": b"wrong"}
        info.addresses = [b"\x0a\x00\x00\x01"]
        info.port = 8080
        mock_zc.get_service_info.return_value = info

        fake_zc = _mock_zeroconf_module()
        with patch.dict(sys.modules, {"zeroconf": fake_zc}):
            mgr._on_service_state_change(mock_zc, "_type", "n1._type", "added")
        assert "n1" not in mgr._neighbors

    def test_added_no_info_ignored(self):
        mgr = CascadeManager(local_id="gw1")
        mock_zc = MagicMock()
        mock_zc.get_service_info.return_value = None
        fake_zc = _mock_zeroconf_module()
        with patch.dict(sys.modules, {"zeroconf": fake_zc}):
            mgr._on_service_state_change(mock_zc, "_type", "n1._type", "added")
        assert len(mgr._neighbors) == 0

    def test_removed_deletes_neighbor(self):
        mgr = CascadeManager(local_id="gw1")
        mgr._neighbors["n1"] = NeighborInfo("n1", "h", 80)
        fake_zc = _mock_zeroconf_module()
        with patch.dict(sys.modules, {"zeroconf": fake_zc}):
            mgr._on_service_state_change(MagicMock(), "_type", "n1._edgelite._tcp.local.", "removed")
        assert "n1" not in mgr._neighbors

    def test_removed_no_match_silent(self):
        mgr = CascadeManager(local_id="gw1")
        mgr._neighbors["n1"] = NeighborInfo("n1", "h", 80)
        fake_zc = _mock_zeroconf_module()
        with patch.dict(sys.modules, {"zeroconf": fake_zc}):
            mgr._on_service_state_change(MagicMock(), "_type", "other._type", "removed")
        assert "n1" in mgr._neighbors  # 未删除

    def test_callback_exception_silent(self):
        mgr = CascadeManager(local_id="gw1")
        fake_zc = _mock_zeroconf_module()
        # get_service_info 抛异常
        mock_zc = MagicMock()
        mock_zc.get_service_info.side_effect = RuntimeError("boom")
        with patch.dict(sys.modules, {"zeroconf": fake_zc}):
            mgr._on_service_state_change(mock_zc, "_type", "n1._type", "added")  # 不应抛

    def test_added_uses_name_when_no_local_id(self):
        mgr = CascadeManager(local_id="gw1")
        mock_zc = MagicMock()
        info = MagicMock()
        info.properties = {}  # 无 local_id
        info.addresses = [b"\x0a\x00\x00\x02"]
        info.port = 9090
        mock_zc.get_service_info.return_value = info
        fake_zc = _mock_zeroconf_module()
        with patch.dict(sys.modules, {"zeroconf": fake_zc}):
            mgr._on_service_state_change(mock_zc, "_type", "fallbackname._type", "added")
        # local_id 为空时用 name 作为 neighbor_id
        assert any(n.port == 9090 for n in mgr._neighbors.values())


# ── 拓扑重建 ──


class TestRebuildTopology:
    def test_updates_peers(self):
        mgr = CascadeManager(local_id="gw1")
        mgr._neighbors["n1"] = NeighborInfo("n1", "h1", 80)
        mgr._neighbors["n2"] = NeighborInfo("n2", "h2", 81)
        mgr._rebuild_topology()
        assert len(mgr._topology.peers) == 2

    def test_parent_found(self):
        mgr = CascadeManager(local_id="gw1", parent_host="parent", parent_port=9000)
        mgr._topology.parent_id = "parent:9000"
        mgr._neighbors["parent"] = NeighborInfo("parent", "parent", 9000, role="child")
        mgr._rebuild_topology()
        assert "parent" in mgr._topology.children

    def test_parent_not_found(self):
        mgr = CascadeManager(local_id="gw1")
        mgr._topology.parent_id = "missing:9000"
        mgr._rebuild_topology()  # 不应抛异常


class TestBuildTopology:
    def test_returns_topology(self):
        mgr = CascadeManager(local_id="gw1")
        mgr._neighbors["n1"] = NeighborInfo("n1", "h", 80)
        topo = mgr.build_topology()
        assert topo.local_id == "gw1"
        assert len(topo.peers) == 1


# ── 邻居发现 ──


class TestDiscoverNeighbors:
    async def test_no_zeroconf_returns_snapshot(self):
        mgr = CascadeManager(local_id="gw1")
        mgr._neighbors["n1"] = NeighborInfo("n1", "h", 80)
        result = await mgr.discover_neighbors(timeout=0.01)
        assert len(result) == 1

    async def test_with_zeroconf_sleeps_and_returns(self):
        mgr = CascadeManager(local_id="gw1")
        mgr._neighbors["n1"] = NeighborInfo("n1", "h", 80)
        mgr._zeroconf = MagicMock()  # 非 None
        original_sleep = asyncio.sleep

        async def _fast(delay, *a, **kw):
            await original_sleep(0)

        with patch("asyncio.sleep", new=_fast):
            result = await mgr.discover_neighbors(timeout=0.01)
        assert len(result) == 1


# ── 父节点转发 ──


class TestForwardToParent:
    async def test_no_parent_returns_false(self):
        mgr = CascadeManager(local_id="gw1")
        assert await mgr.forward_to_parent({"data": 1}) is False

    async def test_hop_limit_exceeded_returns_false(self):
        mgr = CascadeManager(local_id="gw1", parent_host="p", parent_port=9000)
        data = {"_cascade_hop_count": 100}
        assert await mgr.forward_to_parent(data) is False

    async def test_import_error_returns_false(self):
        mgr = CascadeManager(local_id="gw1", parent_host="p", parent_port=9000)
        with patch.dict(sys.modules, {"aiohttp": None}):
            assert await mgr.forward_to_parent({"data": 1}) is False

    async def test_success_returns_true(self):
        mgr = CascadeManager(local_id="gw1", parent_host="p", parent_port=9000, cascade_token="secret")
        mock_session = AsyncMock()
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_session.post = MagicMock(return_value=AsyncMockContext(mock_resp))
        mock_session.closed = False

        fake_aiohttp = MagicMock()
        fake_aiohttp.ClientSession = MagicMock(return_value=mock_session)
        fake_aiohttp.ClientTimeout = MagicMock()

        with patch.dict(sys.modules, {"aiohttp": fake_aiohttp}):
            result = await mgr.forward_to_parent({"data": 1})
        assert result is True
        # 清理会话
        if mgr._http_session:
            mgr._http_session.close = AsyncMock()
            await mgr._http_session.close()

    async def test_non_200_returns_false(self):
        mgr = CascadeManager(local_id="gw1", parent_host="p", parent_port=9000)
        mock_session = AsyncMock()
        mock_resp = AsyncMock()
        mock_resp.status = 500
        mock_session.post = MagicMock(return_value=AsyncMockContext(mock_resp))
        mock_session.closed = False

        fake_aiohttp = MagicMock()
        fake_aiohttp.ClientSession = MagicMock(return_value=mock_session)
        fake_aiohttp.ClientTimeout = MagicMock()

        with patch.dict(sys.modules, {"aiohttp": fake_aiohttp}):
            result = await mgr.forward_to_parent({"data": 1})
        assert result is False

    async def test_exception_returns_false(self):
        mgr = CascadeManager(local_id="gw1", parent_host="p", parent_port=9000)
        fake_aiohttp = MagicMock()
        fake_aiohttp.ClientSession = MagicMock(side_effect=RuntimeError("conn fail"))
        fake_aiohttp.ClientTimeout = MagicMock()
        with patch.dict(sys.modules, {"aiohttp": fake_aiohttp}):
            result = await mgr.forward_to_parent({"data": 1})
        assert result is False

    async def test_increments_hop_count(self):
        mgr = CascadeManager(local_id="gw1", parent_host="p", parent_port=9000)
        mock_session = AsyncMock()
        mock_resp = AsyncMock()
        mock_resp.status = 200
        # 捕获发送的数据
        sent_data = {}

        class _Ctx:
            def __init__(self, resp):
                self._resp = resp

            async def __aenter__(self):
                return self._resp

            async def __aexit__(self, *a):
                return None

        def _post(url, data=None, **kw):
            sent_data["data"] = data
            return _Ctx(mock_resp)

        mock_session.post = _post
        mock_session.closed = False
        fake_aiohttp = MagicMock()
        fake_aiohttp.ClientSession = MagicMock(return_value=mock_session)
        fake_aiohttp.ClientTimeout = MagicMock()

        with patch.dict(sys.modules, {"aiohttp": fake_aiohttp}):
            await mgr.forward_to_parent({"data": 1, "_cascade_hop_count": 2})
        # hop_count 应递增为 3
        import json as _json

        body = _json.loads(sent_data["data"])
        assert body["_cascade_hop_count"] == 3


class AsyncMockContext:
    """辅助：模拟 aiohttp 的 async with session.post(...) 响应上下文。"""

    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return None


# ── 配置更新 ──


class TestUpdateConfig:
    async def test_update_parent(self):
        mgr = CascadeManager(local_id="gw1")
        await mgr.update_config({"parent_host": "newparent", "parent_port": 9001})
        assert mgr._parent_host == "newparent"
        assert mgr._parent_port == 9001
        assert mgr._topology.status == TopologyStatus.CHILD
        assert mgr._topology.parent_id == "newparent:9001"

    async def test_update_scheme_https(self):
        mgr = CascadeManager(local_id="gw1")
        await mgr.update_config({"parent_scheme": "HTTPS"})
        assert mgr._parent_scheme == "https"

    async def test_update_scheme_invalid_ignored(self):
        mgr = CascadeManager(local_id="gw1")
        mgr._parent_scheme = "http"
        await mgr.update_config({"parent_scheme": "ftp"})
        assert mgr._parent_scheme == "http"

    async def test_update_role(self):
        mgr = CascadeManager(local_id="gw1")
        await mgr.update_config({"role": "parent"})
        assert mgr._topology.status == TopologyStatus.PARENT

    async def test_update_role_invalid_ignored(self):
        mgr = CascadeManager(local_id="gw1")
        await mgr.update_config({"role": "bogus"})
        assert mgr._topology.status == TopologyStatus.STANDALONE

    async def test_update_no_parent_clears_role(self):
        mgr = CascadeManager(local_id="gw1")
        mgr._parent_host = "old"
        await mgr.update_config({"parent_host": None, "role": "peer"})
        assert mgr._parent_host is None


# ── 移除邻居 ──


class TestRemoveNeighbor:
    async def test_remove_existing(self):
        mgr = CascadeManager(local_id="gw1")
        mgr._neighbors["n1"] = NeighborInfo("n1", "h", 80)
        assert await mgr.remove_neighbor("n1") is True
        assert "n1" not in mgr._neighbors

    async def test_remove_nonexistent(self):
        mgr = CascadeManager(local_id="gw1")
        assert await mgr.remove_neighbor("unknown") is False


# ── 父节点连接维护 ──


class TestMaintainParentConnection:
    async def test_heartbeat_loop_terminates(self):
        mgr = CascadeManager(local_id="gw1", parent_host="p", parent_port=9000)
        mgr._running = True
        original_sleep = asyncio.sleep
        call_count = 0

        async def _fast_sleep(delay, *a, **kw):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                mgr._running = False
            await original_sleep(0)

        with patch("asyncio.sleep", new=_fast_sleep), patch.object(
            mgr, "forward_to_parent", new=AsyncMock(return_value=True)
        ):
            await asyncio.wait_for(mgr._maintain_parent_connection(), timeout=3)

    async def test_heartbeat_forward_failure_logged(self):
        mgr = CascadeManager(local_id="gw1", parent_host="p", parent_port=9000)
        mgr._running = True
        original_sleep = asyncio.sleep
        call_count = 0

        async def _fast_sleep(delay, *a, **kw):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                mgr._running = False
            await original_sleep(0)

        with patch("asyncio.sleep", new=_fast_sleep), patch.object(
            mgr, "forward_to_parent", new=AsyncMock(return_value=False)
        ):
            await asyncio.wait_for(mgr._maintain_parent_connection(), timeout=3)

    async def test_heartbeat_exception_silent(self):
        mgr = CascadeManager(local_id="gw1", parent_host="p", parent_port=9000)
        mgr._running = True
        original_sleep = asyncio.sleep
        call_count = 0

        async def _fast_sleep(delay, *a, **kw):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                mgr._running = False
            await original_sleep(0)

        async def _boom(data):
            raise RuntimeError("boom")

        with patch("asyncio.sleep", new=_fast_sleep), patch.object(
            mgr, "forward_to_parent", new=_boom
        ):
            await asyncio.wait_for(mgr._maintain_parent_connection(), timeout=3)
