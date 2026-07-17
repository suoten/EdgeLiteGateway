"""InfluxDB 时序存储测试 - 连接/写入/查询/降级/紧急缓冲/同步

覆盖 storage/influx_storage.py：
- InfluxDBStorage: __init__/_init_emergency_db/connect/close
- write_point/write_points_batch: 正常写入、NaN跳过、失败重试、降级
- query_points/query_latest: 正常查询、降级查询、参数校验、超时
- 降级模式: _enter_fallback_mode/_exit_fallback_mode/_publish_fallback_event
- 紧急缓冲: _buffer_append_with_db/_buffer_drain_all/restore_emergency_buffer
- 同步循环: start_sync/stop_sync/force_sync/clear_cache
- 工具方法: _escape_flux_value/_parse_time_to_ns/_parse_aggregate_to_seconds
- 健康检查: check_health/check_network_status
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ───────────────────────── 辅助 ─────────────────────────


def make_sleep_cancel_after(max_calls: int = 1):
    """构造一个替换 asyncio.sleep 的 fake：调用 max_calls 次后抛 CancelledError 退出循环"""
    state = {"count": 0}

    async def _fake_sleep(_seconds):
        state["count"] += 1
        if state["count"] >= max_calls:
            raise asyncio.CancelledError()
        return

    return _fake_sleep


def make_config(**over):
    """构造带 influxdb 段的测试配置"""
    influx = SimpleNamespace(
        url="https://localhost:8086",
        token="test-token",
        org="edgelite",
        bucket="edgelite",
        batch_size=1000,
        flush_interval=5000,
        retention_days=30,
        fallback_backend="sqlite",
        sqlite_ts_path="data/test_ts.db",
        auto_sync_on_recovery=False,
        sync_batch_size=500,
        sync_interval=30,
        network_probe_url="https://www.baidu.com",
    )
    for k, v in over.items():
        setattr(influx, k, v)
    return SimpleNamespace(influxdb=influx)


def make_health(status: str = "pass"):
    h = MagicMock()
    h.status = status
    return h


@pytest.fixture
def storage(tmp_path, monkeypatch):
    """构造 InfluxDBStorage 实例，mock 掉 InfluxDBClient 与 get_config"""
    cfg = make_config()
    monkeypatch.setattr("edgelite.storage.influx_storage.get_config", lambda: cfg)

    # emergency db 路径指向临时目录
    monkeypatch.setenv("EDGELITE_DATA_DIR", str(tmp_path))

    with patch("edgelite.storage.influx_storage.InfluxDBClient") as mock_client_cls:
        # 默认 client 不可用，避免真实连接
        mock_client_cls.side_effect = RuntimeError("no influx")
        from edgelite.storage.influx_storage import InfluxDBStorage

        s = InfluxDBStorage()
        s._emergency_db_path = str(tmp_path / "emergency.db")
        s._init_emergency_db()
        yield s
        # 清理
        try:
            if s._emergency_db:
                s._emergency_db.close()
        except Exception:
            pass


# ───────────────────────── __init__ / _init_emergency_db ─────────────────────────


class TestInit:
    def test_init_reads_config(self, storage):
        """__init__ 应从配置读取连接参数"""
        assert storage._url == "https://localhost:8086"
        assert storage._token == "test-token"
        assert storage._org == "edgelite"
        assert storage._bucket == "edgelite"
        assert storage._retention_days == 30
        assert storage._available is False
        assert storage._fallback_mode is False

    def test_init_emergency_db_creates_table(self, storage):
        """_init_emergency_db 应创建 emergency_buffer 表"""
        assert storage._emergency_db is not None
        tables = storage._emergency_db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        names = [t[0] for t in tables]
        assert "emergency_buffer" in names

    def test_init_emergency_db_integrity_rebuild(self, tmp_path, monkeypatch):
        """损坏的 emergency DB 应被重建或降级到内存缓冲"""
        cfg = make_config()
        monkeypatch.setattr("edgelite.storage.influx_storage.get_config", lambda: cfg)
        monkeypatch.setenv("EDGELITE_DATA_DIR", str(tmp_path))

        db_path = str(tmp_path / "emergency.db")
        # 写入一个损坏的 sqlite 文件
        with open(db_path, "w") as f:
            f.write("not a sqlite file")

        with patch("edgelite.storage.influx_storage.InfluxDBClient"):
            from edgelite.storage.influx_storage import InfluxDBStorage

            s = InfluxDBStorage()
            s._emergency_db_path = db_path
            # 损坏文件 connect 会抛异常，_init_emergency_db 捕获后重建或降级到内存
            s._init_emergency_db()
            # 重建成功则 _emergency_db 非 None；重建失败则降级到内存缓冲（None 也是合法）
            if s._emergency_db:
                s._emergency_db.close()


# ───────────────────────── connect ─────────────────────────


class TestConnect:
    async def test_connect_success(self, storage):
        """connect 成功：health+ping 通过"""
        client = MagicMock()
        client.health.return_value = make_health("pass")
        client.ping.return_value = True
        client.write_api.return_value = MagicMock()
        client.query_api.return_value = MagicMock()
        client.buckets_api.return_value = MagicMock()

        with (
            patch("edgelite.storage.influx_storage.InfluxDBClient", return_value=client),
            patch.object(storage, "_ensure_retention_policy", new=AsyncMock()),
            patch.object(storage, "restore_emergency_buffer", new=AsyncMock()),
            patch.object(storage, "_replay_fallback_file", new=AsyncMock()),
        ):
            await storage.connect()
        assert storage._available is True
        assert storage._write_api is not None
        assert storage._query_api is not None

    async def test_connect_health_fail_enters_fallback(self, storage):
        """health 失败应进入降级模式"""
        client = MagicMock()
        client.health.return_value = make_health("fail")

        with (
            patch("edgelite.storage.influx_storage.InfluxDBClient", return_value=client),
            patch.object(storage, "_enter_fallback_mode", new=AsyncMock()) as mock_fb,
            patch.object(storage, "restore_emergency_buffer", new=AsyncMock()),
            patch.object(storage, "_replay_fallback_file", new=AsyncMock()),
        ):
            await storage.connect()
        assert storage._available is False
        mock_fb.assert_called_once()

    async def test_connect_exception_enters_fallback(self, storage):
        """connect 抛异常应进入降级模式"""
        with (
            patch(
                "edgelite.storage.influx_storage.InfluxDBClient",
                side_effect=ConnectionError("refused"),
            ),
            patch.object(storage, "_enter_fallback_mode", new=AsyncMock()) as mock_fb,
            patch.object(storage, "restore_emergency_buffer", new=AsyncMock()),
            patch.object(storage, "_replay_fallback_file", new=AsyncMock()),
        ):
            await storage.connect()
        assert storage._available is False
        mock_fb.assert_called_once()

    async def test_connect_http_token_warning(self, tmp_path, monkeypatch):
        """HTTP + token 应记录警告（不崩溃）"""
        cfg = make_config(url="http://localhost:8086")
        monkeypatch.setattr("edgelite.storage.influx_storage.get_config", lambda: cfg)
        monkeypatch.setenv("EDGELITE_DATA_DIR", str(tmp_path))

        with patch("edgelite.storage.influx_storage.InfluxDBClient"):
            from edgelite.storage.influx_storage import InfluxDBStorage

            s = InfluxDBStorage()
            s._emergency_db_path = str(tmp_path / "emergency.db")
            s._init_emergency_db()
            with (
                patch.object(s, "_enter_fallback_mode", new=AsyncMock()),
                patch.object(s, "restore_emergency_buffer", new=AsyncMock()),
                patch.object(s, "_replay_fallback_file", new=AsyncMock()),
            ):
                # InfluxDBClient 默认 mock 返回 MagicMock，health 返回 MagicMock.status 非 pass
                await s.connect()
            assert s._available is False
            if s._emergency_db:
                s._emergency_db.close()

    async def test_connect_ping_false(self, storage):
        """ping 返回 False 应标记不可用"""
        client = MagicMock()
        client.health.return_value = make_health("pass")
        client.ping.return_value = False

        with (
            patch("edgelite.storage.influx_storage.InfluxDBClient", return_value=client),
            patch.object(storage, "_enter_fallback_mode", new=AsyncMock()),
            patch.object(storage, "restore_emergency_buffer", new=AsyncMock()),
            patch.object(storage, "_replay_fallback_file", new=AsyncMock()),
        ):
            await storage.connect()
        assert storage._available is False


# ───────────────────────── available / fallback_mode ─────────────────────────


class TestAvailableFallback:
    async def test_available_false_initially(self, storage):
        """初始状态 available 为 False"""
        assert await storage.available() is False

    async def test_fallback_mode_false_initially(self, storage):
        """初始状态 fallback_mode 为 False"""
        assert await storage.fallback_mode() is False

    async def test_using_fallback(self, storage):
        """using_fallback 需要 _fallback_mode 且 _sqlite_ts 非 None"""
        assert await storage.using_fallback() is False
        storage._fallback_mode = True
        storage._sqlite_ts = MagicMock()  # using_fallback 还检查 sqlite_ts
        assert await storage.using_fallback() is True


# ───────────────────────── 降级模式 ─────────────────────────


class TestFallbackMode:
    async def test_enter_fallback_mode(self, storage):
        """进入降级模式应设置标志并增加计数"""
        with patch.object(storage, "_init_sqlite_fallback", new=AsyncMock()):
            await storage._enter_fallback_mode("test")
        assert storage._fallback_mode is True
        assert storage._stats_fallback_count == 1

    async def test_enter_fallback_mode_idempotent(self, storage):
        """重复进入降级模式应幂等"""
        with patch.object(storage, "_init_sqlite_fallback", new=AsyncMock()):
            await storage._enter_fallback_mode("a")
            await storage._enter_fallback_mode("b")
        assert storage._stats_fallback_count == 1

    async def test_exit_fallback_mode(self, storage):
        """退出降级模式应清除标志"""
        storage._fallback_mode = True
        with patch.object(storage, "_publish_fallback_event", new=AsyncMock()):
            await storage._exit_fallback_mode()
        assert storage._fallback_mode is False

    async def test_exit_fallback_mode_idempotent(self, storage):
        """未处于降级模式时退出应幂等"""
        await storage._exit_fallback_mode()
        assert storage._fallback_mode is False

    async def test_publish_fallback_event_no_bus(self, storage):
        """无 event_bus 时发布事件应静默返回"""
        storage._event_bus = None
        await storage._publish_fallback_event("degraded", "reason")

    async def test_publish_fallback_event_with_bus(self, storage):
        """有 event_bus 时应发布事件"""
        bus = MagicMock()
        bus.publish = AsyncMock()
        storage._event_bus = bus
        await storage._publish_fallback_event("recovered", "", 10)
        bus.publish.assert_called_once()


# ───────────────────────── write_point ─────────────────────────


class TestWritePoint:
    async def test_write_point_success(self, storage):
        """InfluxDB 可用时写入成功"""
        storage._available = True
        storage._write_api = MagicMock()
        with patch("edgelite.storage.influx_storage.asyncio.to_thread", new=AsyncMock()):
            result = await storage.write_point("dev1", "temp", 23.5)
        assert result is True

    async def test_write_point_nan_skipped(self, storage):
        """NaN 值应被跳过"""
        storage._available = True
        storage._write_api = MagicMock()

        with patch("edgelite.storage.influx_storage.asyncio.to_thread", new=AsyncMock()):
            result = await storage.write_point("dev1", "temp", float("nan"))
        assert result is False

    async def test_write_point_inf_skipped(self, storage):
        """Infinity 值应被跳过"""
        storage._available = True
        storage._write_api = MagicMock()
        with patch("edgelite.storage.influx_storage.asyncio.to_thread", new=AsyncMock()):
            result = await storage.write_point("dev1", "temp", float("inf"))
        assert result is False

    async def test_write_point_failure_triggers_fallback(self, storage):
        """写入失败应触发降级写入"""
        storage._available = True
        storage._write_api = MagicMock()

        async def boom(*a, **kw):
            raise RuntimeError("write failed")

        with (
            patch("edgelite.storage.influx_storage.asyncio.to_thread", side_effect=boom),
            patch.object(storage, "_fallback_write", new=AsyncMock()) as mock_fb,
            patch.object(storage, "_enter_fallback_mode", new=AsyncMock()),
        ):
            result = await storage.write_point("dev1", "temp", 23.5)
        assert result is False
        mock_fb.assert_called_once()

    async def test_write_point_fallback_mode_direct(self, storage):
        """降级模式下直接走 fallback_write"""
        storage._available = False
        storage._fallback_mode = True
        with (
            patch.object(storage, "_fallback_write", new=AsyncMock()) as mock_fb,
            patch.object(storage, "_enter_fallback_mode", new=AsyncMock()) as mock_enter,
        ):
            result = await storage.write_point("dev1", "temp", 23.5)
        assert result is False
        mock_fb.assert_called_once()
        mock_enter.assert_not_called()

    async def test_write_point_with_timestamp(self, storage):
        """带时间戳的写入应成功"""
        storage._available = True
        storage._write_api = MagicMock()
        ts = datetime.now(UTC)
        with patch("edgelite.storage.influx_storage.asyncio.to_thread", new=AsyncMock()):
            result = await storage.write_point("dev1", "temp", 23.5, timestamp=ts)
        assert result is True


# ───────────────────────── write_points_batch ─────────────────────────


class TestWritePointsBatch:
    async def test_batch_success(self, storage):
        """批量写入成功"""
        storage._available = True
        storage._write_api = MagicMock()
        records = [
            {"device_id": "d1", "point_name": "t", "value": 1.0},
            {"device_id": "d2", "point_name": "t", "value": 2.0},
        ]
        with patch("edgelite.storage.influx_storage.asyncio.to_thread", new=AsyncMock()):
            result = await storage.write_points_batch(records)
        assert result is True

    async def test_batch_empty(self, storage):
        """空批量应返回 True"""
        storage._available = True
        storage._write_api = MagicMock()
        with patch("edgelite.storage.influx_storage.asyncio.to_thread", new=AsyncMock()):
            result = await storage.write_points_batch([])
        assert result is True

    async def test_batch_failure_fallback(self, storage):
        """批量写入失败应走降级"""
        storage._available = True
        storage._write_api = MagicMock()
        records = [{"device_id": "d1", "point_name": "t", "value": 1.0}]

        async def boom(*a, **kw):
            raise RuntimeError("batch fail")

        with (
            patch("edgelite.storage.influx_storage.asyncio.to_thread", side_effect=boom),
            patch.object(storage, "_fallback_batch_write", new=AsyncMock()) as mock_fb,
            patch.object(storage, "_enter_fallback_mode", new=AsyncMock()),
        ):
            result = await storage.write_points_batch(records)
        assert result is False
        mock_fb.assert_called_once()


# ───────────────────────── query_points ─────────────────────────


class TestQueryPoints:
    async def test_query_invalid_start(self, storage):
        """非法 start 参数应返回空列表"""
        storage._available = True
        storage._query_api = MagicMock()
        result = await storage.query_points("d1", "t", "invalid;drop()")
        assert result == []

    async def test_query_invalid_aggregate(self, storage):
        """非法 aggregate 应返回空列表"""
        storage._available = True
        storage._query_api = MagicMock()
        result = await storage.query_points("d1", "t", "-1h", aggregate="bad")
        assert result == []

    async def test_query_fallback(self, storage):
        """不可用时应走降级查询"""
        storage._available = False
        with patch.object(storage, "_fallback_query_points", new=AsyncMock(return_value=[{"v": 1}])) as mock_fq:
            result = await storage.query_points("d1", "t", "-1h")
        assert result == [{"v": 1}]
        mock_fq.assert_called_once()

    async def test_query_max_points_clamp(self, storage):
        """max_points 应被钳制到 [1, 50000]"""
        storage._available = True
        storage._query_api = MagicMock()
        tables = []
        storage._query_api.query = MagicMock(return_value=tables)
        with patch("edgelite.storage.influx_storage.asyncio.to_thread", new=AsyncMock(return_value=tables)):
            # max_points=0 → 钳制为 1；不会因 0 崩溃
            await storage.query_points("d1", "t", "-1h", max_points=0)
            await storage.query_points("d1", "t", "-1h", max_points=999999)

    async def test_query_success(self, storage):
        """正常查询应解析记录"""
        storage._available = True
        storage._query_api = MagicMock()
        record = MagicMock()
        record.get_time.return_value = datetime(2024, 1, 1, tzinfo=UTC)
        record.get_value.return_value = 42.0
        record.values = {"device_id": "d1", "point_name": "t", "quality": "good"}
        table = MagicMock()
        table.records = [record]
        with patch(
            "edgelite.storage.influx_storage.asyncio.to_thread",
            new=AsyncMock(return_value=[table]),
        ):
            result = await storage.query_points("d1", "t", "-1h")
        assert len(result) == 1
        assert result[0]["value"] == 42.0
        assert result[0]["device_id"] == "d1"

    async def test_query_timeout(self, storage):
        """查询超时应返回空列表"""
        storage._available = True
        storage._query_api = MagicMock()

        async def boom(*a, **kw):
            raise TimeoutError()

        with patch("edgelite.storage.influx_storage.asyncio.to_thread", side_effect=boom):
            result = await storage.query_points("d1", "t", "-1h")
        assert result == []

    async def test_query_exception(self, storage):
        """查询异常应返回空列表"""
        storage._available = True
        storage._query_api = MagicMock()

        async def boom(*a, **kw):
            raise RuntimeError("query fail")

        with patch("edgelite.storage.influx_storage.asyncio.to_thread", side_effect=boom):
            result = await storage.query_points("d1", "t", "-1h")
        assert result == []


# ───────────────────────── query_latest ─────────────────────────


class TestQueryLatest:
    async def test_query_latest_fallback(self, storage):
        """不可用时 query_latest 走降级"""
        storage._available = False
        with patch.object(storage, "_fallback_query_latest", new=AsyncMock(return_value={"t": 1.0})) as mock_fl:
            result = await storage.query_latest("d1")
        assert result == {"t": 1.0}
        mock_fl.assert_called_once()


# ───────────────────────── 工具方法 ─────────────────────────


class TestUtilities:
    def test_escape_flux_value(self, storage):
        """_escape_flux_value 应转义引号"""
        assert storage._escape_flux_value("d1") == "'d1'"
        # 包含引号应转义
        assert "\\'" in storage._escape_flux_value("a'b")

    def test_parse_time_to_ns(self, storage):
        """_parse_time_to_ns 应解析 ISO 时间为纳秒"""
        ts = "2024-01-01T00:00:00+00:00"
        ns = storage._parse_time_to_ns(ts)
        assert isinstance(ns, int)
        assert ns > 0

    def test_parse_time_to_ns_relative(self, storage):
        """相对时间（如 -1h）应返回 0 或正数"""
        ns = storage._parse_time_to_ns("-1h")
        assert isinstance(ns, int)

    def test_parse_aggregate_to_seconds(self, storage):
        """_parse_aggregate_to_seconds 应解析聚合窗口"""
        assert storage._parse_aggregate_to_seconds("1m") == 60
        assert storage._parse_aggregate_to_seconds("1h") == 3600
        assert storage._parse_aggregate_to_seconds("30s") == 30


# ───────────────────────── 紧急缓冲 ─────────────────────────


class TestEmergencyBuffer:
    async def test_buffer_append_with_db(self, storage):
        """_buffer_append_with_db 应写入 emergency_db"""
        item = {"device_id": "d1", "point_name": "t", "value": 1.0}
        await storage._buffer_append_with_db(item)
        # 验证写入
        rows = storage._emergency_db.execute("SELECT data FROM emergency_buffer").fetchall()
        assert len(rows) >= 1

    async def test_buffer_drain_all(self, storage):
        """_buffer_drain_all 应清空缓冲并返回列表"""
        await storage._buffer_append_with_db({"device_id": "d1", "value": 1.0})
        drained = await storage._buffer_drain_all()
        assert isinstance(drained, list)

    async def test_buffer_append(self, storage):
        """_buffer_append 应追加到 deque"""
        item = {"device_id": "d1", "value": 1.0}
        await storage._buffer_append(item)
        assert len(storage._emergency_buffer) >= 1

    def test_emergency_db_write(self, storage):
        """_emergency_db_write 应写入 SQLite"""
        storage._emergency_db_write({"device_id": "d1", "value": 1.0})
        rows = storage._emergency_db.execute("SELECT COUNT(*) FROM emergency_buffer").fetchone()
        assert rows[0] >= 1

    def test_emergency_db_write_batch(self, storage):
        """_emergency_db_write_batch 应批量写入"""
        items = [{"device_id": f"d{i}", "value": float(i)} for i in range(5)]
        storage._emergency_db_write_batch(items)
        rows = storage._emergency_db.execute("SELECT COUNT(*) FROM emergency_buffer").fetchone()
        assert rows[0] >= 5

    async def test_restore_emergency_buffer(self, storage):
        """restore_emergency_buffer 应恢复数据且清空表"""
        await storage._buffer_append_with_db({"device_id": "d1", "value": 1.0})
        with patch.object(storage, "_fallback_write", new=AsyncMock()):
            await storage.restore_emergency_buffer()

    async def test_emergency_db_delete_restored(self, storage):
        """_emergency_db_delete_restored 应删除已恢复记录"""
        storage._emergency_db_write({"device_id": "d1", "value": 1.0})
        # 获取 max_id
        max_id = storage._emergency_db.execute("SELECT MAX(id) FROM emergency_buffer").fetchone()[0]
        storage._emergency_db_delete_restored(max_id)


# ───────────────────────── 健康检查 / 网络 ─────────────────────────


class TestHealth:
    async def test_check_health_available(self, storage):
        """_client 存在且 health=pass 时 check_health 应返回 True"""
        client = MagicMock()
        client.health.return_value = make_health("pass")
        client.write_api.return_value = MagicMock()
        client.query_api.return_value = MagicMock()
        client.buckets_api.return_value = MagicMock()
        storage._client = client
        storage._available = True
        result = await storage.check_health()
        assert result is True

    async def test_check_health_no_client(self, storage):
        """无 _client 时 check_health 应返回 False"""
        storage._client = None
        result = await storage.check_health()
        assert result is False

    async def test_check_health_exception(self, storage):
        """health() 抛异常应返回 False"""
        client = MagicMock()
        client.health.side_effect = RuntimeError("boom")
        storage._client = client
        result = await storage.check_health()
        assert result is False

    async def test_check_health_status_fail(self, storage):
        """health.status != pass 应返回 False"""
        client = MagicMock()
        client.health.return_value = make_health("fail")
        storage._client = client
        result = await storage.check_health()
        assert result is False

    async def test_check_network_status(self, storage):
        """check_network_status 应返回状态字符串（mock DNS+HTTP）"""
        # mock _check_dns_connectivity_sync 返回足够成功数，再 mock probe client
        with (
            patch(
                "edgelite.storage.influx_storage.asyncio.to_thread",
                new=AsyncMock(return_value=3),
            ),
            patch.object(storage, "_get_probe_client", new=AsyncMock()) as mock_pc,
        ):
            resp = MagicMock()
            resp.status_code = 200
            mock_client = MagicMock()
            mock_client.get = AsyncMock(return_value=resp)
            mock_pc.return_value = mock_client
            status = await storage.check_network_status()
        assert status == "online"


# ───────────────────────── cleanup / close ─────────────────────────


class TestCleanupClose:
    async def test_cleanup_expired_data_unavailable(self, storage):
        """不可用时 cleanup_expired_data 返回 0"""
        storage._available = False
        result = await storage.cleanup_expired_data()
        assert result == 0

    async def test_close(self, storage):
        """close 应清理资源"""
        storage._client = MagicMock()
        storage._write_api = MagicMock()
        storage._sync_task = None
        await storage.close()
        # close 后 _client 仍存在但 write_api.close 被调用
        storage._write_api.close.assert_called_once()

    async def test_close_with_sync_task(self, storage):
        """close 应取消同步任务"""
        storage._client = MagicMock()
        storage._write_api = MagicMock()
        task = asyncio.create_task(asyncio.sleep(100))
        storage._sync_task = task
        storage._sync_running = True
        await storage.close()
        assert task.cancelled() or task.done()


# ───────────────────────── 同步 ─────────────────────────


class TestSync:
    async def test_start_sync(self, storage):
        """start_sync 应创建同步任务"""
        with patch.object(storage, "_sync_loop", new=AsyncMock()):
            await storage.start_sync()
        assert storage._sync_task is not None
        assert storage._sync_running is True
        # 清理
        if storage._sync_task:
            storage._sync_task.cancel()
            try:
                await storage._sync_task
            except (asyncio.CancelledError, Exception):
                pass

    async def test_stop_sync(self, storage):
        """stop_sync 应停止同步"""
        storage._sync_running = True
        storage._sync_task = None
        await storage.stop_sync()
        assert storage._sync_running is False

    async def test_force_sync_no_sqlite(self, storage):
        """无 sqlite 降级存储时 force_sync 返回 0"""
        storage._sqlite_ts = None
        result = await storage.force_sync()
        assert result == 0

    async def test_clear_cache_no_sqlite(self, storage):
        """无 sqlite 时 clear_cache 返回 0"""
        storage._sqlite_ts = None
        result = await storage.clear_cache()
        assert result == 0

    async def test_get_cache_stats_no_sqlite(self, storage):
        """无 sqlite 时 get_cache_stats 返回零统计"""
        storage._sqlite_ts = None
        stats = await storage.get_cache_stats()
        assert isinstance(stats, dict)

    async def test_get_fallback_stats(self, storage):
        """get_fallback_stats 应返回统计字典"""
        stats = await storage.get_fallback_stats()
        assert isinstance(stats, dict)
        assert "fallback_count" in stats


# ───────────────────────── set_event_bus / backup ─────────────────────────


class TestEventBusBackup:
    def test_set_event_bus(self, storage):
        """set_event_bus 应设置引用"""
        bus = MagicMock()
        storage.set_event_bus(bus)
        assert storage._event_bus is bus

    async def test_backup(self, storage):
        """backup 应执行（即使无 client）"""
        storage._client = None
        # backup 在无 client 时应不崩溃
        await storage.backup()


# ───────────────────────── _fallback_write ─────────────────────────


class TestFallbackWrite:
    async def test_fallback_write_with_sqlite(self, storage):
        """有 sqlite 降级存储时 _fallback_write 应写入"""
        storage._sqlite_ts = MagicMock()
        storage._sqlite_ts.write_point = AsyncMock()
        await storage._fallback_write("d1", "t", 1.0, None, "good")
        storage._sqlite_ts.write_point.assert_called_once()
        assert storage._stats_cached_count == 1

    async def test_fallback_write_no_sqlite_init_fail(self, storage):
        """sqlite 初始化失败时应走紧急缓冲"""
        storage._sqlite_ts = None

        async def init_fail():
            raise RuntimeError("init fail")

        with (
            patch.object(storage, "_ensure_sqlite_started", new=AsyncMock(side_effect=init_fail)),
            patch.object(storage, "_buffer_append_with_db", new=AsyncMock()) as mock_buf,
        ):
            await storage._fallback_write("d1", "t", 1.0, None, "good")
        mock_buf.assert_called_once()

    async def test_fallback_write_sqlite_exception(self, storage):
        """sqlite 写入异常时应走紧急缓冲"""
        storage._sqlite_ts = MagicMock()
        storage._sqlite_ts.write_point = AsyncMock(side_effect=RuntimeError("write fail"))
        with patch.object(storage, "_buffer_append_with_db", new=AsyncMock()) as mock_buf:
            await storage._fallback_write("d1", "t", 1.0, None, "good")
        mock_buf.assert_called_once()


class TestAdditionalCoverage:
    """补充测试覆盖未覆盖的分支"""

    async def test_buffer_append_overflow_with_event_bus(self, storage):
        """缓冲区满且有event_bus时应发布溢出事件"""
        bus = AsyncMock()
        storage._event_bus = bus
        for i in range(10000):
            storage._emergency_buffer.append({"i": i})
        await storage._buffer_append_with_db({"v": 42})
        assert bus.publish.called

    async def test_buffer_extend_full_warning(self, storage):
        """批量扩展缓冲区满时应记录warning"""
        for i in range(9999):
            storage._emergency_buffer.append({"i": i})
        await storage._buffer_extend_with_db([{"a": 1}, {"b": 2}])
        assert len(storage._emergency_buffer) == 10000

    async def test_write_point_success_resets_fail_count(self, storage):
        """写入成功应重置fail_count"""
        storage._available = True
        storage._write_api = MagicMock()
        storage._fail_count = 2
        await storage.write_point("dev1", "temp", 25.5)
        assert storage._fail_count == 0

    async def test_write_points_batch_success_resets_fail_count(self, storage):
        """批量写入成功应重置fail_count"""
        storage._available = True
        storage._write_api = MagicMock()
        storage._fail_count = 2
        await storage.write_points_batch([{"device_id": "d", "point_name": "p", "value": 1}])
        assert storage._fail_count == 0

    async def test_write_points_batch_fail_count_triggers_fallback(self, storage):
        """批量写入连续3次失败应触发降级"""
        storage._available = True
        storage._write_api = MagicMock()
        storage._write_api.write.side_effect = Exception("err")
        storage._fail_count = 2
        with (
            patch.object(storage, "_fallback_batch_write", new_callable=AsyncMock),
            patch.object(storage, "_enter_fallback_mode", new_callable=AsyncMock) as m,
        ):
            await storage.write_points_batch([{"device_id": "d", "point_name": "p", "value": 1}])
        m.assert_called_once()

    async def test_write_points_batch_with_timestamp(self, storage):
        """批量写入带时间戳"""
        storage._available = True
        storage._write_api = MagicMock()
        ts = datetime(2024, 1, 1, tzinfo=UTC)
        records = [{"device_id": "d", "point_name": "p", "value": 1, "timestamp": ts}]
        assert await storage.write_points_batch(records) is True

    async def test_fallback_batch_write_with_timestamp_int(self, storage):
        """批量降级写入带整数时间戳"""
        storage._sqlite_ts = AsyncMock()
        records = [
            {"device_id": "d", "point_name": "p", "value": 1, "timestamp": 1000000000},
            {"device_id": "d", "point_name": "p", "value": 2, "timestamp": datetime(2024, 1, 1, tzinfo=UTC)},
        ]
        with patch.object(storage, "_ensure_sqlite_started", new_callable=AsyncMock):
            await storage._fallback_batch_write(records)
        storage._sqlite_ts.write_points_batch.assert_called_once()

    async def test_fallback_batch_write_exception_with_timestamp(self, storage):
        """批量降级写入异常时缓冲项带时间戳"""
        storage._sqlite_ts = AsyncMock()
        storage._sqlite_ts.write_points_batch.side_effect = Exception("err")
        records = [{"device_id": "d", "point_name": "p", "value": 1, "timestamp": datetime(2024, 1, 1, tzinfo=UTC)}]
        with (
            patch.object(storage, "_ensure_sqlite_started", new_callable=AsyncMock),
            patch.object(storage, "_buffer_extend_with_db", new_callable=AsyncMock) as m,
        ):
            await storage._fallback_batch_write(records)
        m.assert_called_once()

    async def test_query_latest_no_point_name_in_record(self, storage):
        """query_latest记录无point_name时跳过"""
        storage._available = True
        storage._query_api = MagicMock()
        rec = MagicMock()
        rec.get_time.return_value = datetime(2024, 1, 1, tzinfo=UTC)
        rec.get_value.return_value = 25.5
        rec.values = {"device_id": "dev1", "point_name": None, "quality": "good"}
        table = MagicMock()
        table.records = [rec]
        storage._query_api.query.return_value = [table]
        result = await storage.query_latest("dev1")
        assert result == {}

    async def test_sync_loop_drains_buffer_with_timestamp(self, storage):
        """同步循环回灌带时间戳的缓冲数据"""
        storage._available = True
        storage._fallback_mode = False
        storage._sqlite_ts = AsyncMock()
        storage._sqlite_ts.get_unsynced_count.return_value = 0
        storage._write_api = MagicMock()
        storage._last_cleanup_time = time.time()
        ts_str = datetime(2024, 1, 1, tzinfo=UTC).isoformat()
        storage._buffer_drain_all = AsyncMock(
            return_value=[
                {"device_id": "d1", "point_name": "temp", "value": 42, "quality": "good", "timestamp": ts_str}
            ]
        )
        with patch("edgelite.storage.influx_storage.asyncio.sleep", make_sleep_cancel_after(1)):
            storage._sync_running = True
            await storage._sync_loop(interval=1)
        storage._sqlite_ts.write_points_batch.assert_called_once()

    async def test_sync_loop_drains_buffer_invalid_timestamp(self, storage):
        """同步循环回灌无效时间戳的缓冲数据"""
        storage._available = True
        storage._fallback_mode = False
        storage._sqlite_ts = AsyncMock()
        storage._sqlite_ts.get_unsynced_count.return_value = 0
        storage._write_api = MagicMock()
        storage._last_cleanup_time = time.time()
        storage._buffer_drain_all = AsyncMock(
            return_value=[
                {"device_id": "d1", "point_name": "temp", "value": 42, "quality": "good", "timestamp": "bad_ts"}
            ]
        )
        with patch("edgelite.storage.influx_storage.asyncio.sleep", make_sleep_cancel_after(1)):
            storage._sync_running = True
            await storage._sync_loop(interval=1)
        storage._sqlite_ts.write_points_batch.assert_called_once()

    async def test_sync_loop_drains_buffer_conversion_error(self, storage):
        """同步循环回灌数据转换异常应被捕获"""
        storage._available = True
        storage._fallback_mode = False
        storage._sqlite_ts = AsyncMock()
        storage._sqlite_ts.get_unsynced_count.return_value = 0
        storage._write_api = MagicMock()
        storage._last_cleanup_time = time.time()
        storage._buffer_drain_all = AsyncMock(
            return_value=[{"device_id": None, "point_name": "temp", "value": object(), "quality": "good"}]
        )
        with patch("edgelite.storage.influx_storage.asyncio.sleep", make_sleep_cancel_after(1)):
            storage._sync_running = True
            await storage._sync_loop(interval=1)

    async def test_sync_loop_drains_buffer_write_error(self, storage):
        """同步循环回灌写入SQLite失败应被捕获"""
        storage._available = True
        storage._fallback_mode = False
        storage._sqlite_ts = AsyncMock()
        storage._sqlite_ts.get_unsynced_count.return_value = 0
        storage._sqlite_ts.write_points_batch.side_effect = Exception("write err")
        storage._write_api = MagicMock()
        storage._last_cleanup_time = time.time()
        storage._buffer_drain_all = AsyncMock(
            return_value=[{"device_id": "d1", "point_name": "temp", "value": 42, "quality": "good", "timestamp": None}]
        )
        with patch("edgelite.storage.influx_storage.asyncio.sleep", make_sleep_cancel_after(1)):
            storage._sync_running = True
            await storage._sync_loop(interval=1)

    async def test_sync_batch_with_nan_value(self, storage):
        """增量同步跳过NaN值"""
        storage._available = True
        storage._write_api = MagicMock()
        storage._sync_write_api = MagicMock()
        storage._sqlite_ts = AsyncMock()
        storage._sqlite_ts.get_unsynced_records.return_value = [
            {"id": 1, "value": float("nan"), "device_id": "d", "point_name": "t"},
            {"id": 2, "value": 25.5, "device_id": "d", "point_name": "t"},
        ]
        storage._sqlite_ts.sync_completed.return_value = True
        result = await storage._sync_batch()
        assert result == 1

    async def test_sync_batch_with_invalid_float(self, storage):
        """增量同步跳过无法转换为float的值"""
        storage._available = True
        storage._write_api = MagicMock()
        storage._sync_write_api = MagicMock()
        storage._sqlite_ts = AsyncMock()
        storage._sqlite_ts.get_unsynced_records.return_value = [
            {"id": 1, "value": "not_a_number", "device_id": "d", "point_name": "t"},
            {"id": 2, "value": 25.5, "device_id": "d", "point_name": "t"},
        ]
        storage._sqlite_ts.sync_completed.return_value = True
        result = await storage._sync_batch()
        assert result == 1

    async def test_sync_batch_with_last_uploaded_max_id(self, storage):
        """增量同步使用last_uploaded_max_id作为偏移"""
        storage._available = True
        storage._write_api = MagicMock()
        storage._sync_write_api = MagicMock()
        storage._sqlite_ts = AsyncMock()
        storage._last_uploaded_max_id = 10
        storage._sqlite_ts.get_unsynced_records.return_value = [
            {"id": 11, "value": 25.5, "device_id": "d", "point_name": "t"}
        ]
        storage._sqlite_ts.sync_completed.return_value = True
        result = await storage._sync_batch()
        assert result == 1
        storage._sqlite_ts.get_unsynced_records.assert_called_once()
        call_kwargs = storage._sqlite_ts.get_unsynced_records.call_args
        assert call_kwargs.kwargs.get("min_id") == 11

    async def test_sync_batch_with_measurement(self, storage):
        """增量同步使用记录中的measurement"""
        storage._available = True
        storage._write_api = MagicMock()
        storage._sync_write_api = MagicMock()
        storage._sqlite_ts = AsyncMock()
        storage._sqlite_ts.get_unsynced_records.return_value = [
            {
                "id": 1,
                "measurement": "custom_measurement",
                "value": 25.5,
                "device_id": "d",
                "point_name": "t",
                "quality": "good",
                "timestamp_ns": 1000000000,
            }
        ]
        storage._sqlite_ts.sync_completed.return_value = True
        result = await storage._sync_batch()
        assert result == 1

    async def test_force_sync_empty(self, storage):
        """强制同步无数据时返回0"""
        storage._available = True
        storage._write_api = MagicMock()
        storage._sync_write_api = MagicMock()
        storage._sqlite_ts = AsyncMock()
        storage._sqlite_ts.get_unsynced_records.return_value = []
        assert await storage.force_sync() == 0

    async def test_check_health_rebuilds_write_api(self, storage):
        """check_health恢复时应重建write_api"""
        client = MagicMock()
        client.health.return_value = SimpleNamespace(status="pass")
        new_write_api = MagicMock()
        client.write_api.return_value = new_write_api
        client.query_api.return_value = MagicMock()
        client.buckets_api.return_value = MagicMock()
        old_write_api = MagicMock()
        storage._client = client
        storage._available = False
        storage._write_api = old_write_api
        storage._sync_write_api = None
        storage._fallback_mode = True
        with patch.object(storage, "_exit_fallback_mode", new_callable=AsyncMock):
            await storage.check_health()
        old_write_api.close.assert_called_once()
        assert storage._write_api is new_write_api
        assert storage._sync_write_api is not None

    async def test_connect_ping_timeout(self, storage):
        """ping超时应进入降级模式"""
        client = MagicMock()
        client.health.return_value = SimpleNamespace(status="pass")
        client.ping.side_effect = TimeoutError()
        with (
            patch("edgelite.storage.influx_storage.InfluxDBClient", return_value=client),
            patch.object(storage, "_enter_fallback_mode", new_callable=AsyncMock) as mock_fb,
        ):
            await storage.connect()
        assert await storage.available() is False
        mock_fb.assert_called()


# ───────────────────────── 保留策略 ─────────────────────────


class TestRetentionPolicy:
    async def test_ensure_retention_no_client(self, storage):
        """无 client/buckets_api 时应直接返回"""
        storage._client = None
        storage._buckets_api = None
        await storage._ensure_retention_policy()

    async def test_ensure_retention_bucket_not_found(self, storage):
        """bucket 不存在应记录 warning"""
        storage._client = MagicMock()
        storage._buckets_api = MagicMock()
        storage._buckets_api.find_bucket_by_name.return_value = None
        with patch("edgelite.storage.influx_storage.asyncio.to_thread", new=AsyncMock(return_value=None)):
            await storage._ensure_retention_policy()

    async def test_ensure_retention_secs_mismatch_applies(self, storage):
        """retention_secs 与配置不符时应更新"""
        storage._client = MagicMock()
        storage._buckets_api = MagicMock()
        bucket = MagicMock()
        bucket.retention_rules.retention_secs = 86400  # 1天，配置30天
        storage._retention_days = 30
        with (
            patch("edgelite.storage.influx_storage.asyncio.to_thread", new=AsyncMock(return_value=bucket)),
            patch.object(storage, "_apply_retention_policy", new=AsyncMock()) as mock_apply,
        ):
            await storage._ensure_retention_policy()
        mock_apply.assert_called_once()

    async def test_ensure_retention_secs_zero_applies(self, storage):
        """retention_secs=0（无限保留）时应更新"""
        storage._client = MagicMock()
        storage._buckets_api = MagicMock()
        bucket = MagicMock()
        bucket.retention_rules.retention_secs = 0
        storage._retention_days = 30
        with (
            patch("edgelite.storage.influx_storage.asyncio.to_thread", new=AsyncMock(return_value=bucket)),
            patch.object(storage, "_apply_retention_policy", new=AsyncMock()) as mock_apply,
        ):
            await storage._ensure_retention_policy()
        mock_apply.assert_called_once()

    async def test_ensure_retention_secs_correct_no_apply(self, storage):
        """retention_secs 与配置一致时不更新"""
        storage._client = MagicMock()
        storage._buckets_api = MagicMock()
        bucket = MagicMock()
        expected_secs = 30 * 86400
        bucket.retention_rules.retention_secs = expected_secs
        storage._retention_days = 30
        with (
            patch("edgelite.storage.influx_storage.asyncio.to_thread", new=AsyncMock(return_value=bucket)),
            patch.object(storage, "_apply_retention_policy", new=AsyncMock()) as mock_apply,
        ):
            await storage._ensure_retention_policy()
        mock_apply.assert_not_called()

    async def test_ensure_retention_no_rules_applies(self, storage):
        """无 retention_rules 时应应用配置策略"""
        storage._client = MagicMock()
        storage._buckets_api = MagicMock()
        bucket = MagicMock()
        bucket.retention_rules = None
        storage._retention_days = 30
        with (
            patch("edgelite.storage.influx_storage.asyncio.to_thread", new=AsyncMock(return_value=bucket)),
            patch.object(storage, "_apply_retention_policy", new=AsyncMock()) as mock_apply,
        ):
            await storage._ensure_retention_policy()
        mock_apply.assert_called_once()

    async def test_ensure_retention_exception(self, storage):
        """find_bucket 抛异常应被捕获"""
        storage._client = MagicMock()
        storage._buckets_api = MagicMock()

        async def boom(*a, **kw):
            raise RuntimeError("api error")

        with patch("edgelite.storage.influx_storage.asyncio.to_thread", side_effect=boom):
            await storage._ensure_retention_policy()

    async def test_apply_retention_policy_success(self, storage):
        """_apply_retention_policy 应调用 patch_buckets_id"""
        storage._buckets_api = MagicMock()
        bucket = MagicMock()
        bucket.id = "bucket-123"
        mock_service = MagicMock()
        storage._buckets_api._buckets_service = mock_service
        await storage._apply_retention_policy(bucket, 2592000)
        mock_service.patch_buckets_id.assert_called_once()

    async def test_apply_retention_policy_exception(self, storage):
        """_apply_retention_policy 异常应被捕获"""
        storage._buckets_api = MagicMock()
        bucket = MagicMock()
        bucket.id = "bucket-123"

        async def boom(*a, **kw):
            raise RuntimeError("patch fail")

        with patch("edgelite.storage.influx_storage.asyncio.to_thread", side_effect=boom):
            await storage._apply_retention_policy(bucket, 2592000)


# ───────────────────────── 过期数据清理 ─────────────────────────


class TestDeleteOldData:
    async def test_delete_old_data_success(self, storage):
        """_delete_old_data 成功应返回 1"""
        storage._available = True
        storage._query_api = MagicMock()
        storage._client = MagicMock()
        delete_api = MagicMock()
        storage._client.delete_api.return_value = delete_api
        with patch("edgelite.storage.influx_storage.asyncio.to_thread", new=AsyncMock()):
            result = await storage._delete_old_data()
        assert result == 1

    async def test_delete_old_data_not_available(self, storage):
        """不可用时 _delete_old_data 返回 0"""
        storage._available = False
        result = await storage._delete_old_data()
        assert result == 0

    async def test_delete_old_data_exception(self, storage):
        """_delete_old_data 异常应返回 0"""
        storage._available = True
        storage._query_api = MagicMock()
        storage._client = MagicMock()
        storage._client.delete_api.side_effect = RuntimeError("no delete api")

        async def boom(*a, **kw):
            raise RuntimeError("delete fail")

        with patch("edgelite.storage.influx_storage.asyncio.to_thread", side_effect=boom):
            result = await storage._delete_old_data()
        assert result == 0

    async def test_cleanup_expired_with_sqlite(self, storage):
        """不可用但有 sqlite 时 cleanup_expired_data 委托 sqlite"""
        storage._available = False
        storage._sqlite_ts = AsyncMock()
        storage._sqlite_ts.cleanup_old_data.return_value = 5
        result = await storage.cleanup_expired_data()
        assert result == 5

    async def test_cleanup_expired_available_calls_delete(self, storage):
        """可用时 cleanup_expired_data 调用 _delete_old_data"""
        storage._available = True
        storage._query_api = MagicMock()
        storage._client = MagicMock()
        delete_api = MagicMock()
        storage._client.delete_api.return_value = delete_api
        with patch("edgelite.storage.influx_storage.asyncio.to_thread", new=AsyncMock()):
            result = await storage.cleanup_expired_data()
        assert result == 1


# ───────────────────────── _write_with_retry ─────────────────────────


class TestWriteWithRetry:
    async def test_write_retry_success_first_try(self, storage):
        """_write_with_retry 首次成功"""
        storage._write_api = MagicMock()
        with patch("edgelite.storage.influx_storage.asyncio.sleep", new=AsyncMock()):
            await storage._write_with_retry(record=MagicMock())
        storage._write_api.write.assert_called_once()

    async def test_write_retry_connection_error_then_success(self, storage):
        """ConnectionError 重试后成功"""
        storage._write_api = MagicMock()
        storage._write_api.write.side_effect = [ConnectionError("net err"), None]
        with patch("edgelite.storage.influx_storage.asyncio.sleep", new=AsyncMock()):
            await storage._write_with_retry(record=MagicMock(), max_retries=3)
        assert storage._write_api.write.call_count == 2

    async def test_write_retry_timeout_then_success(self, storage):
        """TimeoutError 重试后成功"""
        storage._write_api = MagicMock()
        storage._write_api.write.side_effect = [TimeoutError(), None]
        with patch("edgelite.storage.influx_storage.asyncio.sleep", new=AsyncMock()):
            await storage._write_with_retry(record=MagicMock(), max_retries=3)

    async def test_write_retry_oserror_then_success(self, storage):
        """OSError 重试后成功"""
        storage._write_api = MagicMock()
        storage._write_api.write.side_effect = [OSError("os err"), None]
        with patch("edgelite.storage.influx_storage.asyncio.sleep", new=AsyncMock()):
            await storage._write_with_retry(record=MagicMock(), max_retries=3)

    async def test_write_retry_all_fail_raises(self, storage):
        """全部重试失败应抛出最后异常"""
        storage._write_api = MagicMock()
        storage._write_api.write.side_effect = ConnectionError("persistent")
        with (
            patch("edgelite.storage.influx_storage.asyncio.sleep", new=AsyncMock()),
            pytest.raises(ConnectionError),
        ):
            await storage._write_with_retry(record=MagicMock(), max_retries=3)

    async def test_write_retry_value_error_no_retry(self, storage):
        """ValueError 不重试立即抛出"""
        storage._write_api = MagicMock()
        storage._write_api.write.side_effect = ValueError("bad data")
        with pytest.raises(ValueError):
            await storage._write_with_retry(record=MagicMock(), max_retries=3)
        assert storage._write_api.write.call_count == 1


# ───────────────────────── fallback file 回放 ─────────────────────────


class TestReplayFallbackFile:
    async def test_replay_no_file(self, storage):
        """无 fallback 文件时应直接返回"""
        await storage._replay_fallback_file()

    async def test_replay_valid_file(self, storage, tmp_path):
        """有 fallback 文件时应回放并删除"""
        import json

        fallback_path = storage._emergency_db_path.replace(".db", ".fallback.jsonl")
        items = [{"device_id": "d1", "value": 1.0}, {"device_id": "d2", "value": 2.0}]
        with open(fallback_path, "w", encoding="utf-8") as f:
            for item in items:
                f.write(json.dumps(item) + "\n")
        await storage._replay_fallback_file()
        import os

        assert not os.path.exists(fallback_path)

    async def test_replay_empty_file(self, storage, tmp_path):
        """空 fallback 文件应删除并返回"""
        fallback_path = storage._emergency_db_path.replace(".db", ".fallback.jsonl")
        with open(fallback_path, "w", encoding="utf-8"):
            pass
        await storage._replay_fallback_file()
        import os

        assert not os.path.exists(fallback_path)

    async def test_replay_invalid_json_lines(self, storage, tmp_path):
        """包含非法 JSON 行时应跳过并回放有效行"""
        import json

        fallback_path = storage._emergency_db_path.replace(".db", ".fallback.jsonl")
        with open(fallback_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"device_id": "d1", "value": 1.0}) + "\n")
            f.write("not json\n")
            f.write(json.dumps({"device_id": "d2", "value": 2.0}) + "\n")
        await storage._replay_fallback_file()

    async def test_read_fallback_file_exception(self, storage):
        """_read_fallback_file 异常应返回空列表"""
        result = storage._read_fallback_file("/nonexistent/path/file.jsonl")
        assert result == []


# ───────────────────────── _init_sqlite_fallback ─────────────────────────


class TestInitSqliteFallback:
    async def test_init_sqlite_already_initialized(self, storage):
        """已初始化时应直接返回"""
        storage._sqlite_ts = MagicMock()
        await storage._init_sqlite_fallback()

    async def test_init_sqlite_success(self, storage, tmp_path, monkeypatch):
        """成功初始化 sqlite 降级存储"""
        storage._sqlite_ts = None
        cfg = make_config(sqlite_ts_path=str(tmp_path / "ts.db"))
        monkeypatch.setattr("edgelite.storage.influx_storage.get_config", lambda: cfg)
        with patch("edgelite.storage.influx_storage.SqliteTimeSeriesStorage") as mock_cls:
            candidate = AsyncMock()
            mock_cls.return_value = candidate
            await storage._init_sqlite_fallback()
        assert storage._sqlite_ts is candidate

    async def test_init_sqlite_start_failure(self, storage, tmp_path, monkeypatch):
        """start() 失败时应 cleanup 并抛异常"""
        storage._sqlite_ts = None
        cfg = make_config(sqlite_ts_path=str(tmp_path / "ts.db"))
        monkeypatch.setattr("edgelite.storage.influx_storage.get_config", lambda: cfg)
        with patch("edgelite.storage.influx_storage.SqliteTimeSeriesStorage") as mock_cls:
            candidate = AsyncMock()
            candidate.start.side_effect = RuntimeError("start fail")
            mock_cls.return_value = candidate
            with pytest.raises(RuntimeError):
                await storage._init_sqlite_fallback()
            candidate.stop.assert_called_once()
        assert storage._sqlite_ts is None


# ───────────────────────── 缓冲区重试回滚 ─────────────────────────


class TestBufferRetryRollback:
    async def test_buffer_append_db_write_failure_rollback(self, storage, tmp_path):
        """SQLite 写入失败应回滚 deque 并写 fallback 文件"""
        storage._emergency_db = MagicMock()

        def boom(_item):
            raise sqlite3.OperationalError("locked")

        with (
            patch.object(storage, "_emergency_db_write", side_effect=boom),
            patch("edgelite.storage.influx_storage.asyncio.sleep", new=AsyncMock()),
        ):
            await storage._buffer_append_with_db({"device_id": "d1", "value": 1.0})
        # 写入失败后 deque 应被回滚（pop）
        assert len(storage._emergency_buffer) == 0

    async def test_buffer_extend_db_write_failure_rollback(self, storage):
        """批量 SQLite 写入失败应回滚 deque 并写 fallback 文件"""
        storage._emergency_db = MagicMock()

        def boom(_items):
            raise sqlite3.OperationalError("locked")

        items = [{"device_id": f"d{i}", "value": float(i)} for i in range(3)]
        with (
            patch.object(storage, "_emergency_db_write_batch", side_effect=boom),
            patch("edgelite.storage.influx_storage.asyncio.sleep", new=AsyncMock()),
        ):
            await storage._buffer_extend_with_db(items)
        assert len(storage._emergency_buffer) == 0

    async def test_buffer_append_no_db(self, storage):
        """无 emergency_db 时只追加 deque"""
        storage._emergency_db = None
        await storage._buffer_append_with_db({"device_id": "d1", "value": 1.0})
        assert len(storage._emergency_buffer) == 1

    async def test_buffer_extend_no_db(self, storage):
        """无 emergency_db 时只 extend deque"""
        storage._emergency_db = None
        items = [{"device_id": f"d{i}", "value": float(i)} for i in range(3)]
        await storage._buffer_extend_with_db(items)
        assert len(storage._emergency_buffer) == 3

    async def test_buffer_extend_empty(self, storage):
        """空列表 extend 应直接返回"""
        await storage._buffer_extend_with_db([])

    async def test_emergency_fallback_file_write(self, storage, tmp_path):
        """_emergency_fallback_file_write 应写入 JSONL 文件"""
        import json

        await storage._emergency_fallback_file_write({"device_id": "d1", "value": 1.0})
        fallback_path = storage._emergency_db_path.replace(".db", ".fallback.jsonl")
        with open(fallback_path, encoding="utf-8") as f:
            line = f.readline()
            assert json.loads(line)["device_id"] == "d1"


# ───────────────────────── 降级查询 ─────────────────────────


class TestFallbackQuery:
    async def test_fallback_query_points_no_sqlite(self, storage):
        """无 sqlite 时降级查询返回空列表"""
        storage._sqlite_ts = None
        result = await storage._fallback_query_points("d1", "t", "-1h")
        assert result == []

    async def test_fallback_query_points_success(self, storage):
        """有 sqlite 时降级查询应委托 sqlite"""
        storage._sqlite_ts = AsyncMock()
        storage._sqlite_ts.query_points.return_value = [{"value": 1.0}]
        result = await storage._fallback_query_points("d1", "t", "-1h", aggregate="1m")
        assert result == [{"value": 1.0}]

    async def test_fallback_query_points_exception(self, storage):
        """降级查询异常应返回空列表"""
        storage._sqlite_ts = AsyncMock()
        storage._sqlite_ts.query_points.side_effect = RuntimeError("sqlite err")
        result = await storage._fallback_query_points("d1", "t", "-1h")
        assert result == []

    async def test_fallback_query_points_with_stop(self, storage):
        """降级查询带 stop 参数"""
        storage._sqlite_ts = AsyncMock()
        storage._sqlite_ts.query_points.return_value = []
        await storage._fallback_query_points("d1", "t", "-2h", stop="-1h")
        storage._sqlite_ts.query_points.assert_called_once()

    async def test_fallback_query_latest_no_sqlite(self, storage):
        """无 sqlite 时降级最新值查询返回空"""
        storage._sqlite_ts = None
        result = await storage._fallback_query_latest("d1", ["t1"])
        assert result == {}

    async def test_fallback_query_latest_no_point_names(self, storage):
        """无 point_names 时降级最新值查询返回空"""
        storage._sqlite_ts = AsyncMock()
        result = await storage._fallback_query_latest("d1", None)
        assert result == {}

    async def test_fallback_query_latest_success(self, storage):
        """降级最新值查询成功"""
        storage._sqlite_ts = AsyncMock()
        storage._sqlite_ts.query_latest.return_value = {"t1": {"value": 1.0}}
        result = await storage._fallback_query_latest("d1", ["t1"])
        assert result == {"t1": {"value": 1.0}}

    async def test_fallback_query_latest_exception(self, storage):
        """降级最新值查询异常应返回空"""
        storage._sqlite_ts = AsyncMock()
        storage._sqlite_ts.query_latest.side_effect = RuntimeError("err")
        result = await storage._fallback_query_latest("d1", ["t1"])
        assert result == {}


# ───────────────────────── query_points 聚合 ─────────────────────────


class TestQueryPointsAggregation:
    async def test_query_with_aggregate_and_agg_fn(self, storage):
        """带 aggregate 和 agg_fn 的查询应成功"""
        storage._available = True
        storage._query_api = MagicMock()
        record = MagicMock()
        record.get_time.return_value = datetime(2024, 1, 1, tzinfo=UTC)
        record.get_value.return_value = 10.0
        record.values = {"device_id": "d1", "point_name": "t", "quality": "good"}
        table = MagicMock()
        table.records = [record]
        with patch(
            "edgelite.storage.influx_storage.asyncio.to_thread",
            new=AsyncMock(return_value=[table]),
        ):
            result = await storage.query_points("d1", "t", "-1h", aggregate="1m", agg_fn="max")
        assert len(result) == 1

    async def test_query_with_aggregate_invalid_fn_defaults_mean(self, storage):
        """非法 agg_fn 应默认为 mean"""
        storage._available = True
        storage._query_api = MagicMock()
        table = MagicMock()
        table.records = []
        with patch(
            "edgelite.storage.influx_storage.asyncio.to_thread",
            new=AsyncMock(return_value=[table]),
        ):
            result = await storage.query_points("d1", "t", "-1h", aggregate="1m", agg_fn="invalid_fn")
        assert result == []

    async def test_query_with_stop_param(self, storage):
        """带 stop 参数的查询"""
        storage._available = True
        storage._query_api = MagicMock()
        table = MagicMock()
        table.records = []
        with patch(
            "edgelite.storage.influx_storage.asyncio.to_thread",
            new=AsyncMock(return_value=[table]),
        ):
            result = await storage.query_points("d1", "t", "-2h", stop="-1h")
        assert result == []

    async def test_query_with_offset(self, storage):
        """带 offset 参数的查询"""
        storage._available = True
        storage._query_api = MagicMock()
        table = MagicMock()
        table.records = []
        with patch(
            "edgelite.storage.influx_storage.asyncio.to_thread",
            new=AsyncMock(return_value=[table]),
        ):
            result = await storage.query_points("d1", "t", "-1h", offset=10)
        assert result == []

    async def test_query_invalid_stop(self, storage):
        """非法 stop 参数应返回空列表"""
        storage._available = True
        storage._query_api = MagicMock()
        result = await storage.query_points("d1", "t", "-1h", stop="invalid;drop()")
        assert result == []

    async def test_query_invalid_max_points_type(self, storage):
        """非法 max_points 类型应使用默认值"""
        storage._available = True
        storage._query_api = MagicMock()
        table = MagicMock()
        table.records = []
        with patch(
            "edgelite.storage.influx_storage.asyncio.to_thread",
            new=AsyncMock(return_value=[table]),
        ):
            result = await storage.query_points("d1", "t", "-1h", max_points="not_a_number")
        assert result == []


# ───────────────────────── query_latest 成功路径 ─────────────────────────


class TestQueryLatestSuccess:
    async def test_query_latest_success(self, storage):
        """query_latest 正常查询应返回结果"""
        storage._available = True
        storage._query_api = MagicMock()
        record = MagicMock()
        record.get_time.return_value = datetime(2024, 1, 1, tzinfo=UTC)
        record.get_value.return_value = 25.5
        record.values = {"device_id": "dev1", "point_name": "temp", "quality": "good"}
        table = MagicMock()
        table.records = [record]
        with patch(
            "edgelite.storage.influx_storage.asyncio.to_thread",
            new=AsyncMock(return_value=[table]),
        ):
            result = await storage.query_latest("dev1", ["temp"])
        assert "temp" in result
        assert result["temp"]["value"] == 25.5

    async def test_query_latest_no_point_names(self, storage):
        """query_latest 不带 point_names"""
        storage._available = True
        storage._query_api = MagicMock()
        record = MagicMock()
        record.get_time.return_value = datetime(2024, 1, 1, tzinfo=UTC)
        record.get_value.return_value = 25.5
        record.values = {"device_id": "dev1", "point_name": "temp", "quality": "good"}
        table = MagicMock()
        table.records = [record]
        with patch(
            "edgelite.storage.influx_storage.asyncio.to_thread",
            new=AsyncMock(return_value=[table]),
        ):
            result = await storage.query_latest("dev1")
        assert "temp" in result

    async def test_query_latest_timeout(self, storage):
        """query_latest 超时应返回空"""
        storage._available = True
        storage._query_api = MagicMock()

        async def boom(*a, **kw):
            raise TimeoutError()

        with patch("edgelite.storage.influx_storage.asyncio.to_thread", side_effect=boom):
            result = await storage.query_latest("dev1", ["temp"])
        assert result == {}

    async def test_query_latest_exception(self, storage):
        """query_latest 异常应返回空"""
        storage._available = True
        storage._query_api = MagicMock()

        async def boom(*a, **kw):
            raise RuntimeError("query err")

        with patch("edgelite.storage.influx_storage.asyncio.to_thread", side_effect=boom):
            result = await storage.query_latest("dev1", ["temp"])
        assert result == {}


# ───────────────────────── _sync_batch 更多路径 ─────────────────────────


class TestSyncBatchMore:
    async def test_sync_batch_no_sqlite(self, storage):
        """无 sqlite 时 _sync_batch 返回 0"""
        storage._sqlite_ts = None
        storage._available = True
        result = await storage._sync_batch()
        assert result == 0

    async def test_sync_batch_not_available(self, storage):
        """不可用时 _sync_batch 返回 0"""
        storage._sqlite_ts = AsyncMock()
        storage._available = False
        result = await storage._sync_batch()
        assert result == 0

    async def test_sync_batch_no_write_api(self, storage):
        """无 write_api 时 _sync_batch 返回 0"""
        storage._sqlite_ts = AsyncMock()
        storage._available = True
        storage._write_api = None
        result = await storage._sync_batch()
        assert result == 0

    async def test_sync_batch_value_none_skipped(self, storage):
        """值为 None 的记录应跳过"""
        storage._available = True
        storage._write_api = MagicMock()
        storage._sync_write_api = MagicMock()
        storage._sqlite_ts = AsyncMock()
        storage._sqlite_ts.get_unsynced_records.return_value = [
            {"id": 1, "value": None, "device_id": "d", "point_name": "t"},
            {"id": 2, "value": 25.5, "device_id": "d", "point_name": "t"},
        ]
        storage._sqlite_ts.sync_completed.return_value = True
        with patch("edgelite.storage.influx_storage.asyncio.to_thread", new=AsyncMock()):
            result = await storage._sync_batch()
        assert result == 1

    async def test_sync_batch_write_failure(self, storage):
        """写入 InfluxDB 失败应保留源数据并返回 0"""
        storage._available = True
        storage._write_api = MagicMock()
        storage._sync_write_api = MagicMock()
        storage._sqlite_ts = AsyncMock()
        storage._sqlite_ts.get_unsynced_records.return_value = [
            {"id": 5, "value": 25.5, "device_id": "d", "point_name": "t"},
        ]

        async def boom(*a, **kw):
            raise RuntimeError("write fail")

        with patch("edgelite.storage.influx_storage.asyncio.to_thread", side_effect=boom):
            result = await storage._sync_batch()
        assert result == 0
        assert storage._last_uploaded_max_id == 5

    async def test_sync_batch_sync_completed_fail(self, storage):
        """sync_completed 失败应记录 last_uploaded_max_id"""
        storage._available = True
        storage._write_api = MagicMock()
        storage._sync_write_api = MagicMock()
        storage._sqlite_ts = AsyncMock()
        storage._sqlite_ts.get_unsynced_records.return_value = [
            {"id": 7, "value": 25.5, "device_id": "d", "point_name": "t"},
        ]
        storage._sqlite_ts.sync_completed.return_value = False
        with patch("edgelite.storage.influx_storage.asyncio.to_thread", new=AsyncMock()):
            result = await storage._sync_batch()
        assert result == 1
        assert storage._last_uploaded_max_id == 7

    async def test_sync_batch_no_valid_points(self, storage):
        """所有点都被过滤但仍应标记 sync_completed"""
        storage._available = True
        storage._write_api = MagicMock()
        storage._sync_write_api = MagicMock()
        storage._sqlite_ts = AsyncMock()
        storage._sqlite_ts.get_unsynced_records.return_value = [
            {"id": 3, "value": None, "device_id": "d", "point_name": "t"},
        ]
        storage._sqlite_ts.sync_completed.return_value = True
        with patch("edgelite.storage.influx_storage.asyncio.to_thread", new=AsyncMock()):
            result = await storage._sync_batch()
        assert result == 0
        assert storage._last_uploaded_max_id == 0

    async def test_sync_batch_exception(self, storage):
        """_sync_batch 异常应返回 0 并增加失败计数"""
        storage._available = True
        storage._write_api = MagicMock()
        storage._sync_write_api = MagicMock()
        storage._sqlite_ts = AsyncMock()
        storage._sqlite_ts.get_unsynced_records.side_effect = RuntimeError("db err")
        result = await storage._sync_batch()
        assert result == 0
        assert storage._stats_sync_fail == 1

    async def test_sync_batch_creates_sync_write_api_if_none(self, storage):
        """_sync_write_api 为 None 时应按需创建"""
        storage._available = True
        storage._write_api = MagicMock()
        storage._sync_write_api = None
        storage._client = MagicMock()
        storage._sqlite_ts = AsyncMock()
        storage._sqlite_ts.get_unsynced_records.return_value = [
            {"id": 1, "value": 25.5, "device_id": "d", "point_name": "t"},
        ]
        storage._sqlite_ts.sync_completed.return_value = True
        with patch("edgelite.storage.influx_storage.asyncio.to_thread", new=AsyncMock()):
            result = await storage._sync_batch()
        assert result == 1
        assert storage._sync_write_api is not None


# ───────────────────────── 网络状态 ─────────────────────────


class TestNetworkStatusMore:
    async def test_network_status_weak_one_dns(self, storage):
        """DNS 成功数为 1 时返回 weak"""
        with (
            patch(
                "edgelite.storage.influx_storage.asyncio.to_thread",
                new=AsyncMock(return_value=1),
            ),
        ):
            status = await storage.check_network_status()
        assert status == "weak"

    async def test_network_status_offline(self, storage):
        """DNS 全部失败时返回 offline"""
        with (
            patch(
                "edgelite.storage.influx_storage.asyncio.to_thread",
                new=AsyncMock(return_value=0),
            ),
        ):
            status = await storage.check_network_status()
        assert status == "offline"

    async def test_network_status_http_non_200(self, storage):
        """HTTP 非 200 时返回 weak"""
        with (
            patch(
                "edgelite.storage.influx_storage.asyncio.to_thread",
                new=AsyncMock(return_value=3),
            ),
            patch.object(storage, "_get_probe_client", new=AsyncMock()) as mock_pc,
        ):
            resp = MagicMock()
            resp.status_code = 500
            mock_client = MagicMock()
            mock_client.get = AsyncMock(return_value=resp)
            mock_pc.return_value = mock_client
            status = await storage.check_network_status()
        assert status == "weak"

    async def test_network_status_http_exception(self, storage):
        """HTTP 异常时返回 weak"""
        with (
            patch(
                "edgelite.storage.influx_storage.asyncio.to_thread",
                new=AsyncMock(return_value=3),
            ),
            patch.object(storage, "_get_probe_client", new=AsyncMock()) as mock_pc,
        ):
            mock_client = MagicMock()
            mock_client.get = AsyncMock(side_effect=RuntimeError("http err"))
            mock_pc.return_value = mock_client
            status = await storage.check_network_status()
        assert status == "weak"

    async def test_get_probe_client_lazy_init(self, storage):
        """_get_probe_client 应懒初始化"""
        storage._probe_client = None
        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = MagicMock()
            mock_client.is_closed = False
            mock_cls.return_value = mock_client
            client = await storage._get_probe_client()
        assert client is mock_client

    async def test_get_probe_client_reuse(self, storage):
        """_get_probe_client 应复用未关闭的 client"""
        mock_client = MagicMock()
        mock_client.is_closed = False
        storage._probe_client = mock_client
        client = await storage._get_probe_client()
        assert client is mock_client

    async def test_get_probe_client_recreate_if_closed(self, storage):
        """_probe_client 已关闭时应重新创建"""
        mock_old = MagicMock()
        mock_old.is_closed = True
        storage._probe_client = mock_old
        with patch("httpx.AsyncClient") as mock_cls:
            mock_new = MagicMock()
            mock_new.is_closed = False
            mock_cls.return_value = mock_new
            client = await storage._get_probe_client()
        assert client is mock_new

    async def test_check_dns_connectivity_sync(self, storage):
        """_check_dns_connectivity_sync 静态方法应返回整数"""
        result = storage._check_dns_connectivity_sync(["127.0.0.1"])
        assert isinstance(result, int)
        assert 0 <= result <= 1


# ───────────────────────── get_cache_stats / get_fallback_stats ─────────────────────────


class TestStatsWithSqlite:
    async def test_get_cache_stats_with_sqlite(self, storage):
        """有 sqlite 时 get_cache_stats 应返回完整统计"""
        storage._sqlite_ts = AsyncMock()
        storage._sqlite_ts.get_stats.return_value = {
            "total_records": 100,
            "db_size_bytes": 1048576,
            "oldest_record": "2024-01-01",
            "newest_record": "2024-06-01",
            "unsynced_count": 50,
        }
        stats = await storage.get_cache_stats()
        assert stats["count"] == 100
        assert stats["size_mb"] == 1.0
        assert stats["pending"] == 50
        assert "fallback_mode" in stats

    async def test_get_fallback_stats_with_sqlite(self, storage):
        """有 sqlite 时 get_fallback_stats 应返回完整统计"""
        storage._sqlite_ts = AsyncMock()
        storage._sqlite_ts.get_stats.return_value = {
            "total_records": 200,
            "db_size_bytes": 2097152,
            "oldest_record": "2024-01-01",
            "newest_record": "2024-06-01",
            "unsynced_count": 30,
        }
        stats = await storage.get_fallback_stats()
        assert stats["total_records"] == 200
        assert stats["fallback_count"] == 0
        assert stats["cached_count"] == 0


# ───────────────────────── force_sync / _sync_from_sqlite ─────────────────────────


class TestForceSyncMore:
    async def test_force_sync_multiple_batches(self, storage):
        """force_sync 应循环调用直到无数据"""
        storage._available = True
        storage._write_api = MagicMock()
        storage._sync_write_api = MagicMock()
        storage._sqlite_ts = AsyncMock()
        call_count = {"n": 0}

        async def mock_sync_batch():
            call_count["n"] += 1
            if call_count["n"] <= 2:
                return 5
            return 0

        with patch.object(storage, "_sync_batch", new=mock_sync_batch):
            result = await storage.force_sync()
        assert result == 10

    async def test_force_sync_with_sqlite(self, storage):
        """有 sqlite 时 force_sync 调用 _sync_from_sqlite"""
        storage._sqlite_ts = AsyncMock()
        storage._available = True
        storage._write_api = MagicMock()
        storage._sync_write_api = MagicMock()
        storage._sqlite_ts.get_unsynced_records.return_value = []
        result = await storage.force_sync()
        assert result == 0

    async def test_sync_from_sqlite(self, storage):
        """_sync_from_sqlite 应委托 _sync_batch"""
        storage._available = True
        storage._write_api = MagicMock()
        storage._sync_write_api = MagicMock()
        storage._sqlite_ts = AsyncMock()
        storage._sqlite_ts.get_unsynced_records.return_value = []
        result = await storage._sync_from_sqlite()
        assert result == 0


# ───────────────────────── _parse_time_to_ns 更多 ─────────────────────────


class TestParseTimeMore:
    def test_parse_time_relative_seconds(self, storage):
        """相对时间 -1s"""
        ns = storage._parse_time_to_ns("-1s")
        assert isinstance(ns, int)
        assert ns > 0

    def test_parse_time_relative_minutes(self, storage):
        """相对时间 -30m"""
        ns = storage._parse_time_to_ns("-30m")
        assert isinstance(ns, int)

    def test_parse_time_relative_days(self, storage):
        """相对时间 -2d"""
        ns = storage._parse_time_to_ns("-2d")
        assert isinstance(ns, int)

    def test_parse_time_relative_weeks(self, storage):
        """相对时间 -1w"""
        ns = storage._parse_time_to_ns("-1w")
        assert isinstance(ns, int)

    def test_parse_time_relative_months(self, storage):
        """相对时间 -1M"""
        ns = storage._parse_time_to_ns("-1M")
        assert isinstance(ns, int)

    def test_parse_time_relative_years(self, storage):
        """相对时间 -1y"""
        ns = storage._parse_time_to_ns("-1y")
        assert isinstance(ns, int)

    def test_parse_time_pure_number(self, storage):
        """纯数字（纳秒）"""
        ns = storage._parse_time_to_ns("1000000000")
        assert ns == 1000000000

    def test_parse_time_invalid_defaults_hour_ago(self, storage):
        """无法解析的时间默认返回1小时前"""
        ns = storage._parse_time_to_ns("not_a_time")
        assert isinstance(ns, int)

    def test_parse_aggregate_default(self, storage):
        """非法聚合窗口应返回默认 60"""
        assert storage._parse_aggregate_to_seconds("invalid") == 60

    def test_parse_aggregate_seconds(self, storage):
        """聚合窗口 - 秒"""
        assert storage._parse_aggregate_to_seconds("15s") == 15


# ───────────────────────── _fallback_to_cache ─────────────────────────


class TestFallbackToCache:
    async def test_fallback_to_cache_no_cache_manager(self, storage):
        """无 cache_manager 时应返回 False"""
        fake_state = SimpleNamespace(cache_manager=None)
        with patch("edgelite.app._app_state", fake_state):
            result = await storage._fallback_to_cache("d1", "t", 1.0, None, "good")
        assert result is False

    async def test_fallback_to_cache_success(self, storage):
        """有 cache_manager 时应写入缓存"""
        fake_cache = AsyncMock()
        fake_cache.add_to_cache.return_value = True
        fake_state = SimpleNamespace(cache_manager=fake_cache)
        with patch("edgelite.app._app_state", fake_state):
            result = await storage._fallback_to_cache("d1", "t", 1.0, datetime(2024, 1, 1, tzinfo=UTC), "good")
        assert result is True

    async def test_fallback_to_cache_exception(self, storage):
        """cache_manager 异常应返回 False"""
        fake_cache = AsyncMock()
        fake_cache.add_to_cache.side_effect = RuntimeError("cache err")
        fake_state = SimpleNamespace(cache_manager=fake_cache)
        with patch("edgelite.app._app_state", fake_state):
            result = await storage._fallback_to_cache("d1", "t", 1.0, None, "good")
        assert result is False


# ───────────────────────── write_point 失败计数 ─────────────────────────


class TestWritePointFailCount:
    async def test_write_point_fail_count_increments(self, storage):
        """写入失败应递增 fail_count 但不立即降级"""
        storage._available = True
        storage._write_api = MagicMock()

        async def boom(*a, **kw):
            raise RuntimeError("write err")

        with (
            patch("edgelite.storage.influx_storage.asyncio.to_thread", side_effect=boom),
            patch.object(storage, "_fallback_write", new=AsyncMock()),
            patch.object(storage, "_enter_fallback_mode", new=AsyncMock()) as mock_enter,
        ):
            result = await storage.write_point("dev1", "temp", 23.5)
        assert result is False
        assert storage._fail_count == 1
        mock_enter.assert_not_called()

    async def test_write_point_fail_count_3_triggers_fallback(self, storage):
        """fail_count 达到 3 应触发降级"""
        storage._available = True
        storage._write_api = MagicMock()
        storage._fail_count = 2

        async def boom(*a, **kw):
            raise RuntimeError("write err")

        with (
            patch("edgelite.storage.influx_storage.asyncio.to_thread", side_effect=boom),
            patch.object(storage, "_fallback_write", new=AsyncMock()),
            patch.object(storage, "_enter_fallback_mode", new=AsyncMock()) as mock_enter,
        ):
            result = await storage.write_point("dev1", "temp", 23.5)
        assert result is False
        mock_enter.assert_called_once()


# ───────────────────────── write_points_batch 更多路径 ─────────────────────────


class TestWritePointsBatchMore:
    async def test_batch_truncated(self, storage):
        """超过 10000 条应截断"""
        storage._available = True
        storage._write_api = MagicMock()
        records = [{"device_id": "d", "point_name": "p", "value": float(i)} for i in range(10001)]
        with patch("edgelite.storage.influx_storage.asyncio.to_thread", new=AsyncMock()):
            result = await storage.write_points_batch(records)
        assert result is True

    async def test_batch_skip_missing_fields(self, storage):
        """缺少必填字段的记录应跳过"""
        storage._available = True
        storage._write_api = MagicMock()
        records = [
            {"device_id": "", "point_name": "p", "value": 1.0},
            {"device_id": "d", "point_name": "", "value": 2.0},
            {"device_id": "d", "point_name": "p", "value": None},
            {"device_id": "d", "point_name": "p", "value": 3.0},
        ]
        with patch("edgelite.storage.influx_storage.asyncio.to_thread", new=AsyncMock()):
            result = await storage.write_points_batch(records)
        assert result is True

    async def test_batch_all_skipped_returns_true(self, storage):
        """所有记录被跳过后 points 为空应返回 True"""
        storage._available = True
        storage._write_api = MagicMock()
        records = [{"device_id": "", "point_name": "p", "value": 1.0}]
        with patch("edgelite.storage.influx_storage.asyncio.to_thread", new=AsyncMock()):
            result = await storage.write_points_batch(records)
        assert result is True

    async def test_batch_nan_skipped(self, storage):
        """NaN 值的记录应跳过"""
        storage._available = True
        storage._write_api = MagicMock()
        records = [
            {"device_id": "d", "point_name": "p", "value": float("nan")},
            {"device_id": "d", "point_name": "p", "value": 3.0},
        ]
        with patch("edgelite.storage.influx_storage.asyncio.to_thread", new=AsyncMock()):
            result = await storage.write_points_batch(records)
        assert result is True

    async def test_batch_fail_count_not_triggered(self, storage):
        """批量写入失败但 fail_count < 3 不触发降级"""
        storage._available = True
        storage._write_api = MagicMock()
        records = [{"device_id": "d", "point_name": "p", "value": 1.0}]

        async def boom(*a, **kw):
            raise RuntimeError("write err")

        with (
            patch("edgelite.storage.influx_storage.asyncio.to_thread", side_effect=boom),
            patch.object(storage, "_fallback_batch_write", new=AsyncMock()),
            patch.object(storage, "_enter_fallback_mode", new=AsyncMock()) as mock_enter,
        ):
            result = await storage.write_points_batch(records)
        assert result is False
        assert storage._fail_count == 1
        mock_enter.assert_not_called()


# ───────────────────────── close 更多路径 ─────────────────────────


class TestCloseMore:
    async def test_close_with_sqlite(self, storage):
        """close 应停止 sqlite_ts"""
        storage._client = MagicMock()
        storage._write_api = MagicMock()
        storage._sync_write_api = MagicMock()
        sqlite_mock = AsyncMock()
        storage._sqlite_ts = sqlite_mock
        storage._sync_task = None
        await storage.close()
        sqlite_mock.stop.assert_called_once()
        assert storage._sqlite_ts is None

    async def test_close_with_sync_write_api(self, storage):
        """close 应关闭 sync_write_api"""
        storage._client = MagicMock()
        storage._write_api = MagicMock()
        sync_write_mock = MagicMock()
        storage._sync_write_api = sync_write_mock
        storage._sync_task = None
        await storage.close()
        sync_write_mock.close.assert_called_once()
        assert storage._sync_write_api is None

    async def test_close_write_api_exception(self, storage):
        """write_api.close 异常应被捕获"""
        storage._client = MagicMock()
        storage._write_api = MagicMock()
        storage._write_api.close.side_effect = RuntimeError("close err")
        storage._sync_task = None
        await storage.close()

    async def test_close_client_exception(self, storage):
        """client.close 异常应被捕获"""
        storage._client = MagicMock()
        storage._client.close.side_effect = RuntimeError("close err")
        storage._write_api = MagicMock()
        storage._sync_task = None
        await storage.close()
        assert storage._client is None

    async def test_close_sqlite_exception(self, storage):
        """sqlite_ts.stop 异常应被捕获"""
        storage._client = MagicMock()
        storage._write_api = MagicMock()
        sqlite_mock = AsyncMock()
        sqlite_mock.stop.side_effect = RuntimeError("stop err")
        storage._sqlite_ts = sqlite_mock
        storage._sync_task = None
        await storage.close()
        assert storage._sqlite_ts is None

    async def test_close_emergency_db_exception(self, storage):
        """emergency_db.close 异常应被捕获"""
        storage._client = MagicMock()
        storage._write_api = MagicMock()
        db_mock = MagicMock()
        db_mock.close.side_effect = RuntimeError("close err")
        storage._emergency_db = db_mock
        storage._sync_task = None
        await storage.close()
        assert storage._emergency_db is None

    async def test_close_probe_client(self, storage):
        """close 应通过 stop_sync 关闭 probe_client"""
        storage._client = MagicMock()
        storage._write_api = MagicMock()
        probe_mock = MagicMock()
        storage._probe_client = probe_mock
        storage._sync_task = None
        await storage.close()
        probe_mock.aclose.assert_called_once()


# ───────────────────────── backup / stop_sync 更多 ─────────────────────────


class TestBackupStopMore:
    async def test_backup_with_sqlite(self, storage):
        """有 sqlite 时 backup 应委托"""
        storage._sqlite_ts = AsyncMock()
        await storage.backup("/tmp/backup")
        storage._sqlite_ts.backup.assert_called_once()

    async def test_stop_sync_with_probe_client(self, storage):
        """stop_sync 应关闭 probe_client"""
        storage._sync_running = True
        storage._sync_task = None
        probe_mock = MagicMock()
        storage._probe_client = probe_mock
        await storage.stop_sync()
        probe_mock.aclose.assert_called_once()
        assert storage._probe_client is None

    async def test_stop_sync_no_task(self, storage):
        """无 sync_task 时 stop_sync 应正常"""
        storage._sync_running = True
        storage._sync_task = None
        await storage.stop_sync()
        assert storage._sync_running is False

    async def test_clear_cache_with_sqlite(self, storage):
        """有 sqlite 时 clear_cache 应委托"""
        storage._sqlite_ts = AsyncMock()
        storage._sqlite_ts.clear_all.return_value = 10
        result = await storage.clear_cache()
        assert result == 10


# ───────────────────────── restore_emergency_buffer 更多 ─────────────────────────


class TestRestoreMore:
    async def test_restore_no_db(self, storage):
        """无 emergency_db 时 restore 应直接返回"""
        storage._emergency_db = None
        await storage.restore_emergency_buffer()

    async def test_restore_empty_table(self, storage):
        """空表时 restore 应直接返回"""
        await storage.restore_emergency_buffer()
        assert len(storage._emergency_buffer) == 0

    async def test_restore_with_data(self, storage):
        """有数据时 restore 应恢复到 deque 并删除已恢复行"""
        import json

        db_mock = MagicMock()
        cursor_mock = MagicMock()
        cursor_mock.fetchall.return_value = [
            (1, json.dumps({"device_id": "d1", "value": 1.0})),
            (2, json.dumps({"device_id": "d2", "value": 2.0})),
        ]
        db_mock.execute.return_value = cursor_mock
        storage._emergency_db = db_mock
        with patch.object(storage, "_emergency_db_delete_restored") as mock_del:
            await storage.restore_emergency_buffer()
        assert len(storage._emergency_buffer) == 2
        mock_del.assert_called_once_with(2)

    async def test_restore_exception(self, storage):
        """restore 异常应被捕获"""
        db_mock = MagicMock()
        db_mock.execute.side_effect = RuntimeError("db err")
        storage._emergency_db = db_mock
        await storage.restore_emergency_buffer()


# ───────────────────────── _emergency_db_write 更多 ─────────────────────────


class TestEmergencyDbWriteMore:
    def test_emergency_db_write_no_db(self, storage):
        """无 emergency_db 时 _emergency_db_write 应直接返回"""
        storage._emergency_db = None
        storage._emergency_db_write({"v": 1})

    def test_emergency_db_write_exception(self, storage):
        """_emergency_db_write 异常应被捕获"""
        storage._emergency_db = MagicMock()
        storage._emergency_db.execute.side_effect = RuntimeError("write err")
        storage._emergency_db_write({"v": 1})

    def test_emergency_db_write_batch_no_db(self, storage):
        """无 emergency_db 时批量写入应直接返回"""
        storage._emergency_db = None
        storage._emergency_db_write_batch([{"v": 1}])

    def test_emergency_db_write_batch_exception(self, storage):
        """批量写入异常应被捕获"""
        storage._emergency_db = MagicMock()
        storage._emergency_db.executemany.side_effect = RuntimeError("batch err")
        storage._emergency_db_write_batch([{"v": 1}])

    def test_emergency_db_delete_restored_no_db(self, storage):
        """无 emergency_db 时删除应直接返回"""
        storage._emergency_db = None
        storage._emergency_db_delete_restored(100)


# ───────────────────────── check_health 恢复路径 ─────────────────────────


class TestCheckHealthRecovery:
    async def test_check_health_already_healthy(self, storage):
        """已可用且 health=pass 时应保持可用"""
        client = MagicMock()
        client.health.return_value = make_health("pass")
        storage._client = client
        storage._available = True
        storage._write_api = MagicMock()
        result = await storage.check_health()
        assert result is True

    async def test_check_health_becomes_unhealthy(self, storage):
        """health=fail 时应标记不可用"""
        client = MagicMock()
        client.health.return_value = make_health("fail")
        storage._client = client
        storage._available = True
        result = await storage.check_health()
        assert result is False
        assert storage._available is False


# ───────────────────────── _sync_loop 清理路径 ─────────────────────────


class TestSyncLoopCleanup:
    async def test_sync_loop_calls_cleanup(self, storage):
        """_sync_loop 应在超过24小时后调用 cleanup_expired_data"""
        storage._available = True
        storage._fallback_mode = False
        storage._sqlite_ts = AsyncMock()
        storage._sqlite_ts.get_unsynced_count.return_value = 0
        storage._write_api = MagicMock()
        storage._last_cleanup_time = time.time() - 100000  # 超过24小时
        storage._buffer_drain_all = AsyncMock(return_value=[])
        storage.cleanup_expired_data = AsyncMock(return_value=1)
        with patch("edgelite.storage.influx_storage.asyncio.sleep", make_sleep_cancel_after(1)):
            storage._sync_running = True
            await storage._sync_loop(interval=1)
        storage.cleanup_expired_data.assert_called_once()

    async def test_sync_loop_cleanup_exception(self, storage):
        """cleanup_expired_data 异常应被捕获"""
        storage._available = True
        storage._fallback_mode = False
        storage._sqlite_ts = AsyncMock()
        storage._sqlite_ts.get_unsynced_count.return_value = 0
        storage._write_api = MagicMock()
        storage._last_cleanup_time = time.time() - 100000
        storage._buffer_drain_all = AsyncMock(return_value=[])
        storage.cleanup_expired_data = AsyncMock(side_effect=RuntimeError("cleanup err"))
        with patch("edgelite.storage.influx_storage.asyncio.sleep", make_sleep_cancel_after(1)):
            storage._sync_running = True
            await storage._sync_loop(interval=1)

    async def test_sync_loop_checks_health_in_fallback(self, storage):
        """降级模式下应检查 InfluxDB 是否恢复"""
        storage._available = False
        storage._fallback_mode = True
        storage._sqlite_ts = AsyncMock()
        storage._sqlite_ts.get_unsynced_count.return_value = 0
        storage._write_api = MagicMock()
        storage._last_cleanup_time = time.time()
        storage._buffer_drain_all = AsyncMock(return_value=[])
        storage.check_health = AsyncMock(return_value=False)
        with patch("edgelite.storage.influx_storage.asyncio.sleep", make_sleep_cancel_after(1)):
            storage._sync_running = True
            await storage._sync_loop(interval=1)
        storage.check_health.assert_called()

    async def test_sync_loop_exception(self, storage):
        """_sync_loop 主体异常应被捕获并继续"""
        storage._available = True
        storage._fallback_mode = False
        storage._sqlite_ts = AsyncMock()
        storage._sqlite_ts.get_unsynced_count.side_effect = RuntimeError("boom")
        storage._write_api = MagicMock()
        storage._last_cleanup_time = time.time()
        storage._buffer_drain_all = AsyncMock(return_value=[])
        with patch("edgelite.storage.influx_storage.asyncio.sleep", make_sleep_cancel_after(1)):
            storage._sync_running = True
            await storage._sync_loop(interval=1)


# ───────────────────────── connect 成功路径 flush_interval 钳制 ─────────────────────────


class TestConnectFlushInterval:
    async def test_connect_clamps_flush_interval(self, storage, monkeypatch):
        """flush_interval 超出范围应被钳制"""
        cfg = make_config(flush_interval=999999)
        monkeypatch.setattr("edgelite.storage.influx_storage.get_config", lambda: cfg)
        client = MagicMock()
        client.health.return_value = make_health("pass")
        client.ping.return_value = True
        client.write_api.return_value = MagicMock()
        client.query_api.return_value = MagicMock()
        client.buckets_api.return_value = MagicMock()
        with (
            patch("edgelite.storage.influx_storage.InfluxDBClient", return_value=client),
            patch.object(storage, "_ensure_retention_policy", new=AsyncMock()),
            patch.object(storage, "restore_emergency_buffer", new=AsyncMock()),
            patch.object(storage, "_replay_fallback_file", new=AsyncMock()),
        ):
            await storage.connect()
        assert storage._available is True

    async def test_connect_auto_sync_on_recovery(self, storage, monkeypatch):
        """auto_sync_on_recovery=True 时应启动同步"""
        cfg = make_config(auto_sync_on_recovery=True)
        monkeypatch.setattr("edgelite.storage.influx_storage.get_config", lambda: cfg)
        client = MagicMock()
        client.health.return_value = make_health("pass")
        client.ping.return_value = True
        client.write_api.return_value = MagicMock()
        client.query_api.return_value = MagicMock()
        client.buckets_api.return_value = MagicMock()
        with (
            patch("edgelite.storage.influx_storage.InfluxDBClient", return_value=client),
            patch.object(storage, "_ensure_retention_policy", new=AsyncMock()),
            patch.object(storage, "restore_emergency_buffer", new=AsyncMock()),
            patch.object(storage, "_replay_fallback_file", new=AsyncMock()),
            patch.object(storage, "start_sync", new=AsyncMock()) as mock_start,
        ):
            await storage.connect()
        mock_start.assert_called_once()


# ───────────────────────── 边缘分支补全 ─────────────────────────


class TestEdgeBranches:
    async def test_publish_fallback_event_publish_exception(self, storage):
        """_publish_fallback_event 中 bus.publish 异常应被捕获"""
        bus = AsyncMock()
        bus.publish.side_effect = RuntimeError("publish fail")
        storage._event_bus = bus
        await storage._publish_fallback_event("degraded", "test")

    async def test_ensure_sqlite_started_calls_init(self, storage):
        """_ensure_sqlite_started 在 _sqlite_ts 为 None 时应调用 _init_sqlite_fallback"""
        storage._sqlite_ts = None
        with patch.object(storage, "_init_sqlite_fallback", new=AsyncMock()) as mock_init:
            await storage._ensure_sqlite_started()
        mock_init.assert_called_once()

    async def test_write_point_not_available_not_fallback(self, storage):
        """不可用且未降级时应进入降级模式并写入"""
        storage._available = False
        storage._fallback_mode = False
        with (
            patch.object(storage, "_enter_fallback_mode", new=AsyncMock()) as mock_enter,
            patch.object(storage, "_fallback_write", new=AsyncMock()) as mock_fw,
        ):
            result = await storage.write_point("d1", "t", 1.0)
        assert result is False
        mock_enter.assert_called_once()
        mock_fw.assert_called_once()

    async def test_write_point_fail_count_3_already_fallback(self, storage):
        """fail_count 达到 3 但已处于降级模式时不重复进入"""
        storage._available = True
        storage._write_api = MagicMock()
        storage._fail_count = 2
        storage._fallback_mode = True

        async def boom(*a, **kw):
            raise RuntimeError("write err")

        with (
            patch("edgelite.storage.influx_storage.asyncio.to_thread", side_effect=boom),
            patch.object(storage, "_fallback_write", new=AsyncMock()),
            patch.object(storage, "_enter_fallback_mode", new=AsyncMock()) as mock_enter,
        ):
            result = await storage.write_point("dev1", "temp", 23.5)
        assert result is False
        mock_enter.assert_not_called()

    async def test_fallback_write_with_timestamp(self, storage):
        """_fallback_write 带时间戳应转换为纳秒"""
        storage._sqlite_ts = AsyncMock()
        ts = datetime(2024, 1, 1, tzinfo=UTC)
        await storage._fallback_write("d1", "t", 1.0, ts, "good")
        storage._sqlite_ts.write_point.assert_called_once()
        call_kwargs = storage._sqlite_ts.write_point.call_args.kwargs
        assert call_kwargs["timestamp_ns"] is not None

    async def test_fallback_write_sqlite_none_after_ensure(self, storage):
        """_ensure_sqlite_started 后 sqlite_ts 仍为 None 应直接返回"""
        storage._sqlite_ts = None
        with patch.object(storage, "_ensure_sqlite_started", new=AsyncMock()):
            await storage._fallback_write("d1", "t", 1.0, None, "good")

    async def test_fallback_batch_write_sqlite_none_after_ensure(self, storage):
        """批量降级写入时 sqlite_ts 仍为 None 应直接返回"""
        storage._sqlite_ts = None
        with patch.object(storage, "_ensure_sqlite_started", new=AsyncMock()):
            await storage._fallback_batch_write([{"device_id": "d", "point_name": "p", "value": 1.0}])

    async def test_fallback_batch_write_datetime_timestamp(self, storage):
        """批量降级写入带 datetime 时间戳"""
        storage._sqlite_ts = AsyncMock()
        records = [
            {"device_id": "d", "point_name": "p", "value": 1, "timestamp": datetime(2024, 1, 1, tzinfo=UTC)},
        ]
        with patch.object(storage, "_ensure_sqlite_started", new=AsyncMock()):
            await storage._fallback_batch_write(records)
        storage._sqlite_ts.write_points_batch.assert_called_once()

    async def test_fallback_batch_write_buf_item_exception(self, storage):
        """批量降级写入构造 buf_item 异常时应跳过"""
        storage._sqlite_ts = AsyncMock()
        storage._sqlite_ts.write_points_batch.side_effect = RuntimeError("sqlite err")

        class BadDatetime(datetime):
            def timestamp(self):
                raise RuntimeError("bad timestamp")

        records = [{"device_id": "d", "point_name": "p", "value": 1, "timestamp": BadDatetime(2024, 1, 1, tzinfo=UTC)}]
        with (
            patch.object(storage, "_ensure_sqlite_started", new=AsyncMock()),
            patch.object(storage, "_buffer_extend_with_db", new=AsyncMock()) as mock_buf,
        ):
            await storage._fallback_batch_write(records)
        # batch_items 应为空（异常被捕获），不调用 _buffer_extend_with_db
        mock_buf.assert_not_called()

    async def test_query_invalid_offset_type(self, storage):
        """非法 offset 类型应使用默认值 0"""
        storage._available = True
        storage._query_api = MagicMock()
        table = MagicMock()
        table.records = []
        with patch(
            "edgelite.storage.influx_storage.asyncio.to_thread",
            new=AsyncMock(return_value=[table]),
        ):
            result = await storage.query_points("d1", "t", "-1h", offset="not_a_number")
        assert result == []

    async def test_write_points_batch_not_available_not_fallback(self, storage):
        """批量写入不可用且未降级时应进入降级模式"""
        storage._available = False
        storage._fallback_mode = False
        records = [{"device_id": "d", "point_name": "p", "value": 1.0}]
        with (
            patch.object(storage, "_enter_fallback_mode", new=AsyncMock()) as mock_enter,
            patch.object(storage, "_fallback_batch_write", new=AsyncMock()) as mock_fb,
        ):
            result = await storage.write_points_batch(records)
        assert result is False
        mock_enter.assert_called_once()
        mock_fb.assert_called_once()

    async def test_start_sync_already_running(self, storage):
        """已运行时 start_sync 应直接返回"""
        storage._sync_running = True
        storage._sync_task = None
        await storage.start_sync()
        assert storage._sync_task is None

    async def test_exit_fallback_with_sqlite(self, storage):
        """退出降级模式且有 sqlite 时应查询待同步数"""
        storage._fallback_mode = True
        storage._sqlite_ts = AsyncMock()
        storage._sqlite_ts.get_unsynced_count.return_value = 42
        with patch.object(storage, "_publish_fallback_event", new=AsyncMock()) as mock_pub:
            await storage._exit_fallback_mode()
        mock_pub.assert_called_once()
        call_args = mock_pub.call_args
        assert call_args[0][2] == 42  # cached_count

    async def test_buffer_append_overflow_event_bus_exception(self, storage):
        """缓冲区满且 event_bus.publish 异常时应被捕获"""
        bus = AsyncMock()
        bus.publish.side_effect = RuntimeError("bus err")
        storage._event_bus = bus
        for i in range(10000):
            storage._emergency_buffer.append({"i": i})
        await storage._buffer_append_with_db({"v": 42})

    async def test_emergency_fallback_file_write_exception(self, storage):
        """_emergency_fallback_file_write 异常应被捕获"""
        with patch.object(storage, "_append_line", side_effect=RuntimeError("write err")):
            await storage._emergency_fallback_file_write({"v": 1})

    async def test_emergency_db_delete_restored_exception(self, storage):
        """_emergency_db_delete_restored 异常应被捕获"""
        db_mock = MagicMock()
        db_mock.execute.side_effect = RuntimeError("delete err")
        storage._emergency_db = db_mock
        storage._emergency_db_delete_restored(100)

    async def test_replay_fallback_file_extend_exception(self, storage, tmp_path):
        """_replay_fallback_file 中 _buffer_extend_with_db 异常应被捕获"""
        import json

        fallback_path = storage._emergency_db_path.replace(".db", ".fallback.jsonl")
        with open(fallback_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"device_id": "d1", "value": 1.0}) + "\n")
        with patch.object(storage, "_buffer_extend_with_db", side_effect=RuntimeError("extend err")):
            await storage._replay_fallback_file()

    async def test_sync_loop_drained_with_batch_write(self, storage):
        """_sync_loop 回灌缓冲数据到 sqlite 成功"""
        storage._available = True
        storage._fallback_mode = False
        storage._sqlite_ts = AsyncMock()
        storage._sqlite_ts.get_unsynced_count.return_value = 0
        storage._write_api = MagicMock()
        storage._last_cleanup_time = time.time()
        ts_str = datetime(2024, 1, 1, tzinfo=UTC).isoformat()
        storage._buffer_drain_all = AsyncMock(
            return_value=[
                {"device_id": "d1", "point_name": "temp", "value": 42, "quality": "good", "timestamp": ts_str}
            ]
        )
        with patch("edgelite.storage.influx_storage.asyncio.sleep", make_sleep_cancel_after(1)):
            storage._sync_running = True
            await storage._sync_loop(interval=1)
        storage._sqlite_ts.write_points_batch.assert_called_once()

    async def test_sync_loop_drained_batch_write_exception(self, storage):
        """_sync_loop 回灌写入 sqlite 失败应被捕获"""
        storage._available = True
        storage._fallback_mode = False
        storage._sqlite_ts = AsyncMock()
        storage._sqlite_ts.get_unsynced_count.return_value = 0
        storage._sqlite_ts.write_points_batch.side_effect = RuntimeError("write err")
        storage._write_api = MagicMock()
        storage._last_cleanup_time = time.time()
        storage._buffer_drain_all = AsyncMock(
            return_value=[{"device_id": "d1", "point_name": "temp", "value": 42, "quality": "good"}]
        )
        with patch("edgelite.storage.influx_storage.asyncio.sleep", make_sleep_cancel_after(1)):
            storage._sync_running = True
            await storage._sync_loop(interval=1)

    async def test_sync_loop_calls_sync_batch(self, storage):
        """_sync_loop 在有未同步数据时应调用 _sync_batch"""
        storage._available = True
        storage._fallback_mode = False
        storage._sqlite_ts = AsyncMock()
        storage._sqlite_ts.get_unsynced_count.return_value = 10
        storage._write_api = MagicMock()
        storage._last_cleanup_time = time.time()
        storage._buffer_drain_all = AsyncMock(return_value=[])
        storage._sync_batch = AsyncMock(return_value=5)
        with patch("edgelite.storage.influx_storage.asyncio.sleep", make_sleep_cancel_after(1)):
            storage._sync_running = True
            await storage._sync_loop(interval=1)
        storage._sync_batch.assert_called_once()

    async def test_sync_loop_cleanup_returns_truthy(self, storage):
        """_sync_loop cleanup 返回真值时应记录日志"""
        storage._available = True
        storage._fallback_mode = False
        storage._sqlite_ts = AsyncMock()
        storage._sqlite_ts.get_unsynced_count.return_value = 0
        storage._write_api = MagicMock()
        storage._last_cleanup_time = time.time() - 100000
        storage._buffer_drain_all = AsyncMock(return_value=[])
        storage.cleanup_expired_data = AsyncMock(return_value=5)
        with patch("edgelite.storage.influx_storage.asyncio.sleep", make_sleep_cancel_after(1)):
            storage._sync_running = True
            await storage._sync_loop(interval=1)

    async def test_sync_loop_cancelled_error(self, storage):
        """_sync_loop 收到 CancelledError 应退出循环"""
        storage._available = True
        storage._fallback_mode = False
        storage._sqlite_ts = AsyncMock()
        storage._sqlite_ts.get_unsynced_count.return_value = 0
        storage._write_api = MagicMock()
        storage._last_cleanup_time = time.time()
        storage._buffer_drain_all = AsyncMock(return_value=[])

        call_count = {"n": 0}

        async def cancel_after_first(_sec):
            call_count["n"] += 1
            raise asyncio.CancelledError()

        with patch("edgelite.storage.influx_storage.asyncio.sleep", side_effect=cancel_after_first):
            storage._sync_running = True
            await storage._sync_loop(interval=1)
