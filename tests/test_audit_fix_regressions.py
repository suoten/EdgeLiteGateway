"""审计修复回归测试（第二轮补充）——覆盖首轮新增路径中未测到的部分

对应修复:
- EventBus 周期性 outbox 重放（start_periodic_replay / shutdown 取消）
- CertManager.days_until_expiry（证书过期巡检的数据源）
- OTAManager.download_update 断点续传与非 200/206 错误分支
- ProtocolBridgeManager 配置持久化（save/load/损坏容错）
- InferenceScheduler 超时请求取消跳过 + 队列满同锁判定
- api.data._validate_time_range 公共时间范围校验
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException


# ── EventBus 周期性 outbox 重放 ────────────────────────────────────────


class TestEventBusPeriodicReplay:
    @pytest.mark.asyncio
    async def test_periodic_replay_invokes_and_shutdown_cancels(self):
        from edgelite.engine.event_bus import EventBus

        bus = EventBus()
        bus._alarm_outbox = object()  # 打开持久化开关（周期重放的前置条件）
        calls = {"n": 0}

        async def fake_replay() -> int:
            calls["n"] += 1
            return 0

        bus.replay_pending_alarms = fake_replay  # type: ignore[method-assign]

        bus.start_periodic_replay(interval_seconds=0.05)
        await asyncio.sleep(0.18)
        first = calls["n"]
        assert first >= 2, "周期重放应至少触发两次"

        await bus.shutdown()
        settled = calls["n"]
        await asyncio.sleep(0.12)
        assert calls["n"] == settled, "shutdown 后周期重放任务应被取消"

    def test_start_without_outbox_is_noop(self):
        from edgelite.engine.event_bus import EventBus

        bus = EventBus()
        # 未 enable_alarm_persistence 时静默不启动、不报错
        bus.start_periodic_replay(interval_seconds=0.01)
        assert bus._periodic_replay_task is None


# ── CertManager.days_until_expiry ──────────────────────────────────────


class TestCertDaysUntilExpiry:
    def _make_cert(self, path, days: int) -> None:
        from datetime import datetime as dt

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "edgelite-test")])
        now = dt.now(UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=days))
            .sign(key, hashes.SHA256())
        )
        path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    def test_days_until_expiry_positive(self, tmp_path):
        from edgelite.engine.tls_security import CertManager

        cert_path = tmp_path / "server.pem"
        self._make_cert(cert_path, days=365)
        mgr = CertManager(cert_dir=str(tmp_path))
        days = mgr.days_until_expiry(cert_path)
        assert days is not None and 360 <= days <= 365

    def test_missing_or_invalid_returns_none(self, tmp_path):
        from edgelite.engine.tls_security import CertManager

        mgr = CertManager(cert_dir=str(tmp_path))
        assert mgr.days_until_expiry(tmp_path / "missing.pem") is None
        bad = tmp_path / "bad.pem"
        bad.write_bytes(b"not a certificate")
        assert mgr.days_until_expiry(bad) is None


# ── OTAManager.download_update（断点续传 / 错误分支）────────────────────


class _FakeStreamCM:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        return False


class _FakeResponse:
    def __init__(self, status_code: int, chunks: list[bytes]):
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self._chunks = chunks

    async def aiter_bytes(self, chunk_size: int = 8192):
        for chunk in self._chunks:
            yield chunk


class _FakeClient:
    def __init__(self, response: _FakeResponse):
        self._response = response
        self.captured_headers: dict | None = None
        self.captured_url: str | None = None

    def stream(self, method: str, url: str, headers: dict | None = None):
        self.captured_headers = headers
        self.captured_url = url
        return _FakeStreamCM(self._response)


class TestOtaDownloadUpdate:
    def _make_mgr(self, tmp_path, response: _FakeResponse):
        from edgelite.engine.ota_manager import OTAManager

        mgr = OTAManager()
        mgr._upgrade_dir = tmp_path  # 隔离：不写仓库 data/ota
        mgr._client = _FakeClient(response)
        return mgr

    @pytest.mark.asyncio
    async def test_http_error_returns_none(self, tmp_path):
        mgr = self._make_mgr(tmp_path, _FakeResponse(404, [b"not found"]))
        result = await mgr.download_update("1.2.0", "http://ota.example/x.zip")
        assert result is None
        assert not (tmp_path / "update-1.2.0.zip").exists()

    @pytest.mark.asyncio
    async def test_resume_from_partial_with_range(self, tmp_path):
        part = tmp_path / "update-1.2.0.zip.part"
        part.write_bytes(b"ABCD")
        mgr = self._make_mgr(tmp_path, _FakeResponse(206, [b"EFGH"]))
        result = await mgr.download_update("1.2.0", "http://ota.example/x.zip")
        assert result == tmp_path / "update-1.2.0.zip"
        assert result.read_bytes() == b"ABCDEFGH"
        client: _FakeClient = mgr._client  # type: ignore[assignment]
        assert client.captured_headers is not None
        assert client.captured_headers.get("Range") == "bytes=4-"
        assert not part.exists(), "完成后 .part 应被 replace 掉"

    @pytest.mark.asyncio
    async def test_no_part_downloads_from_scratch(self, tmp_path):
        mgr = self._make_mgr(tmp_path, _FakeResponse(200, [b"XYZ"]))
        result = await mgr.download_update("1.2.0", "http://ota.example/x.zip")
        assert result is not None
        assert result.read_bytes() == b"XYZ"
        client: _FakeClient = mgr._client  # type: ignore[assignment]
        assert not (client.captured_headers or {}).get("Range")


# ── ProtocolBridgeManager 持久化 ───────────────────────────────────────


class TestProtocolBridgePersistence:
    def test_save_load_roundtrip(self, tmp_path):
        from edgelite.engine.protocol_bridge import (
            MappingRule,
            ProtocolBridge,
            ProtocolBridgeManager,
        )

        m1 = ProtocolBridgeManager()
        pfile = tmp_path / "bridges.json"
        m1._persist_path = pfile
        rule = MappingRule(
            rule_id="r1",
            source_protocol="modbus_tcp",
            target_protocol="opcua",
            source_device="d1",
            source_point="temp",
            target_device="d2",
            target_point="Temp",
            data_type="scale",
            scale=2.5,
            offset=1.0,
            enabled=False,
        )
        m1.add_bridge(
            ProtocolBridge(
                bridge_id="b1",
                source_protocol="modbus_tcp",
                target_protocol="opcua",
                source_config={"unit_id": 1},
                target_config={},
                mapping_rules=[rule],
                enabled=False,
            )
        )
        m1.save_config()
        assert pfile.exists()

        m2 = ProtocolBridgeManager()
        m2._persist_path = pfile
        assert m2.load_config() == 1
        bridge = m2.get_bridge_by_id("b1")
        assert bridge is not None
        assert bridge.enabled is False
        assert bridge.source_config == {"unit_id": 1}
        assert bridge.mapping_rules[0].scale == 2.5
        assert bridge.mapping_rules[0].enabled is False

    def test_load_corrupt_file_returns_zero(self, tmp_path):
        from edgelite.engine.protocol_bridge import ProtocolBridgeManager

        pfile = tmp_path / "bridges.json"
        pfile.write_text("{corrupt json", encoding="utf-8")
        m = ProtocolBridgeManager()
        m._persist_path = pfile
        assert m.load_config() == 0

    def test_load_missing_file_returns_zero(self, tmp_path):
        from edgelite.engine.protocol_bridge import ProtocolBridgeManager

        m = ProtocolBridgeManager()
        m._persist_path = tmp_path / "nonexistent.json"
        assert m.load_config() == 0


# ── InferenceScheduler 取消跳过 / 队列满 ───────────────────────────────


class TestInferenceSchedulerCancelAndQueue:
    @pytest.mark.asyncio
    async def test_cancelled_request_skipped_not_executed(self):
        from edgelite.engine.inference_scheduler import (
            InferencePriority,
            InferenceScheduler,
            _InferenceRequest,
        )

        s = InferenceScheduler(max_concurrent=1, max_queue_size=10)
        executed = {"n": 0}

        async def infer_fn(_input):
            executed["n"] += 1
            return 1.0

        await s.register_model("m", infer_fn)
        await s.start()
        try:
            loop = asyncio.get_running_loop()
            fut = loop.create_future()
            req = _InferenceRequest(
                model_id="m",
                input_data=[1.0],
                priority=InferencePriority.USER_QUERY,
                future=fut,
            )
            req.cancelled = True  # 模拟调用方已超时放弃
            async with s._queue_lock:
                s._pending_queue.append(req)

            await asyncio.sleep(0.25)
            assert executed["n"] == 0, "已取消的请求不应被执行"
            assert fut.cancelled()
        finally:
            await s.stop()

    @pytest.mark.asyncio
    async def test_queue_full_rejected_atomic(self):
        from edgelite.engine.inference_scheduler import (
            InferencePriority,
            InferenceScheduler,
        )

        s = InferenceScheduler(max_concurrent=1, max_queue_size=1)

        async def infer_fn(_input):
            return 1.0

        await s.register_model("m", infer_fn)
        # 不启动 dispatch 循环（手动置 running），排除"出队快于入队"的干扰，
        # 纯粹验证满队列判定与入队在同一锁临界区内
        s._running = True
        try:
            with pytest.raises(TimeoutError):
                await s.submit_and_wait("m", [1.0], InferencePriority.USER_QUERY, timeout=0.15)
            with pytest.raises(RuntimeError, match="queue full"):
                await s.submit_and_wait("m", [2.0], InferencePriority.USER_QUERY, timeout=0.15)
        finally:
            s._running = False


# ── api.data._validate_time_range ──────────────────────────────────────


class TestValidateTimeRange:
    def _now(self):
        return datetime.now(UTC)

    def test_valid_range_passes(self):
        from edgelite.api.data import _validate_time_range

        now = self._now()
        _validate_time_range((now - timedelta(days=1)).isoformat(), (now - timedelta(hours=1)).isoformat())
        _validate_time_range((now - timedelta(days=10)).isoformat(), None)

    def test_start_after_stop_rejected(self):
        from edgelite.api.data import _validate_time_range

        now = self._now()
        with pytest.raises(HTTPException) as exc:
            _validate_time_range(now.isoformat(), (now - timedelta(days=1)).isoformat())
        assert exc.value.status_code == 400

    def test_range_over_90d_rejected(self):
        from edgelite.api.data import _validate_time_range

        now = self._now()
        with pytest.raises(HTTPException) as exc:
            _validate_time_range(
                (now - timedelta(days=92)).isoformat(),
                (now - timedelta(days=1)).isoformat(),
            )
        assert exc.value.status_code == 400

    def test_start_alone_over_90d_rejected(self):
        from edgelite.api.data import _validate_time_range

        now = self._now()
        with pytest.raises(HTTPException) as exc:
            _validate_time_range((now - timedelta(days=91)).isoformat(), None)
        assert exc.value.status_code == 400
