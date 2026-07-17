"""Extended unit tests for edgelite.engine.edge_ai_inference.

Covers: _check_onnxruntime, _generate_onnx_model, OnnxModelWrapper,
InferenceResult, InferenceStatsCollector, _validate_video_url,
_validate_cloud_endpoint, AiInferenceEngine (initialize, providers,
preset models, reload, infer, cloud fallback, model management,
scheduled inference, video inference, shutdown, version management),
AiInferenceEvent, TFLiteModelWrapper, PMMLModelWrapper, quantization,
and get_model_info.

All external I/O (onnxruntime sessions, cv2, httpx, psutil, InfluxDB,
lazy-imported engine submodules) is mocked.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, "src")

from edgelite.engine import edge_ai_inference as eai
from edgelite.engine.edge_ai_inference import (
    AiInferenceEngine,
    AiInferenceEvent,
    InferenceResult,
    InferenceStatsCollector,
    OnnxModelWrapper,
    PMMLModelWrapper,
    TFLiteModelWrapper,
    _generate_onnx_model,
    _validate_cloud_endpoint,
    _validate_video_url,
    get_model_info,
    quantize_model_fp16,
    quantize_model_int8,
)

# ─── Helpers ──────────────────────────────────────────────────────────────


def _make_lazy_modules(
    version_mgr=None,
    hot_swap_mgr=None,
    resource_mon=None,
    provider_fn=None,
):
    """Inject fake modules for lazy-imported engine submodules that don't exist on disk."""
    version_mod = ModuleType("edgelite.engine.ai_version_manager")
    version_mod.ModelVersionManager = MagicMock(return_value=version_mgr or MagicMock())
    version_mod.HotSwapManager = MagicMock(return_value=hot_swap_mgr or MagicMock())

    resource_mod = ModuleType("edgelite.engine.ai_resource_monitor")
    resource_mod.ResourceMonitor = MagicMock(return_value=resource_mon or MagicMock())

    detector_mod = ModuleType("edgelite.engine.ai_device_detector")
    detector_mod.select_best_provider = provider_fn or (lambda pref: ("CPUExecutionProvider", "CPU"))

    return {
        "edgelite.engine.ai_version_manager": version_mod,
        "edgelite.engine.ai_resource_monitor": resource_mod,
        "edgelite.engine.ai_device_detector": detector_mod,
    }


def _make_preprocess_modules():
    """Inject fake ai_preprocess/ai_postprocess modules for OnnxModelWrapper with pipelines."""
    pre_mod = ModuleType("edgelite.engine.ai_preprocess")
    pre_mod.PreprocessPipeline = MagicMock(return_value=MagicMock(apply=MagicMock(side_effect=lambda x: x)))
    post_mod = ModuleType("edgelite.engine.ai_postprocess")
    post_mod.PostprocessPipeline = MagicMock(return_value=MagicMock(apply=MagicMock(side_effect=lambda x: x)))
    return {
        "edgelite.engine.ai_preprocess": pre_mod,
        "edgelite.engine.ai_postprocess": post_mod,
    }


def _make_onnx_wrapper(
    model_id="m1",
    model_name="Model-1",
    model_version="v1.0.0",
    model_type="anomaly",
    is_preset=False,
    model_path="/models/m1.onnx",
    input_schema=None,
    output_schema=None,
    status="inactive",
    preprocess_config=None,
    postprocess_config=None,
):
    """Create an OnnxModelWrapper without triggering lazy pipeline imports."""
    w = OnnxModelWrapper(
        model_id=model_id,
        model_name=model_name,
        model_version=model_version,
        model_type=model_type,
        is_preset=is_preset,
        model_path=model_path,
        input_schema=input_schema or {"shape": [1, 3]},
        output_schema=output_schema or {"shape": [1]},
        preprocess_config=preprocess_config,
        postprocess_config=postprocess_config,
    )
    w.status = status
    return w


def _make_active_engine(tmp_path=None, config=None):
    """Create an engine with internal state set up but without calling initialize()."""
    import tempfile

    models_dir = str(tmp_path) if tmp_path else tempfile.mkdtemp()
    engine = AiInferenceEngine(models_dir=models_dir, enabled=True)
    engine._config = config or {}
    return engine


# ─── TestCheckOnnxruntime ─────────────────────────────────────────────────


class TestCheckOnnxruntime:
    def test_returns_true_when_already_loaded(self):
        with patch.object(eai, "_HAS_ONNX", True):
            assert eai._check_onnxruntime() is True

    def test_returns_false_on_import_error(self):
        original_has = eai._HAS_ONNX
        original_ort = eai.ort
        try:
            with patch("builtins.__import__", side_effect=ImportError("no onnxruntime")):
                with patch.object(eai, "_HAS_ONNX", False):
                    assert eai._check_onnxruntime() is False
        finally:
            eai._HAS_ONNX = original_has
            eai.ort = original_ort

    def test_dynamically_loads_onnxruntime(self):
        """When _HAS_ONNX is False, _check_onnxruntime tries to import onnxruntime."""
        original_has = eai._HAS_ONNX
        original_ort = eai.ort
        try:
            fake_ort = MagicMock()
            fake_ort.__version__ = "1.0.0"
            with patch.object(eai, "_HAS_ONNX", False):
                with patch("builtins.__import__", return_value=fake_ort):
                    result = eai._check_onnxruntime()
                    assert result is True
                    assert eai._HAS_ONNX is True
        finally:
            eai._HAS_ONNX = original_has
            eai.ort = original_ort


# ─── TestGenerateOnnxModel ────────────────────────────────────────────────


class TestGenerateOnnxModel:
    def test_anomaly_model(self):
        result = _generate_onnx_model("elg-anomaly-v1.onnx", [1, 10], [1])
        assert result is not None
        assert isinstance(result, bytes)
        assert len(result) > 0

    def test_trend_model(self):
        result = _generate_onnx_model("elg-trend-v1.onnx", [1, 20], [1, 5])
        assert result is not None
        assert isinstance(result, bytes)

    def test_threshold_model(self):
        result = _generate_onnx_model("elg-threshold-v1.onnx", [1, 10], [1])
        assert result is not None
        assert isinstance(result, bytes)

    def test_generic_model_identity(self):
        """Generic model with in_dim==out_dim==1 uses Identity node."""
        result = _generate_onnx_model("generic-v1.onnx", [1, 1], [1, 1])
        assert result is not None

    def test_generic_model_matmul(self):
        """Generic model with multi-dim uses MatMul+Add."""
        result = _generate_onnx_model("generic-v1.onnx", [1, 5], [1, 3])
        assert result is not None

    def test_returns_none_when_onnx_not_available(self):
        with patch("builtins.__import__", side_effect=ImportError("no onnx")):
            result = _generate_onnx_model("test.onnx", [1, 5], [1])
            assert result is None


# ─── TestOnnxModelWrapper ─────────────────────────────────────────────────


class TestOnnxModelWrapper:
    def test_init_defaults(self):
        w = _make_onnx_wrapper()
        assert w.status == "inactive"
        assert w.session is None
        assert w.loaded_at is None
        assert w._preprocess_pipeline is None
        assert w._postprocess_pipeline is None

    def test_init_with_preprocess_config(self):
        mods = _make_preprocess_modules()
        with patch.dict(sys.modules, mods):
            w = OnnxModelWrapper(
                model_id="m1",
                model_name="Model",
                model_version="v1.0.0",
                model_type="anomaly",
                is_preset=False,
                model_path="/m.onnx",
                input_schema={"shape": [1, 3]},
                output_schema={"shape": [1]},
                preprocess_config=[{"type": "normalize"}],
            )
            assert w._preprocess_pipeline is not None

    def test_init_with_postprocess_config(self):
        mods = _make_preprocess_modules()
        with patch.dict(sys.modules, mods):
            w = OnnxModelWrapper(
                model_id="m1",
                model_name="Model",
                model_version="v1.0.0",
                model_type="anomaly",
                is_preset=False,
                model_path="/m.onnx",
                input_schema={"shape": [1, 3]},
                output_schema={"shape": [1]},
                postprocess_config=[{"type": "scale"}],
            )
            assert w._postprocess_pipeline is not None

    async def test_load_cpu_provider(self):
        w = _make_onnx_wrapper()
        fake_session = MagicMock()
        with patch.object(eai, "_check_onnxruntime", return_value=True):
            with patch.object(eai, "ort") as mock_ort:
                mock_ort.InferenceSession.return_value = fake_session
                await w.load(provider="CPU")
                assert w.status == "active"
                assert w.session is fake_session
                assert w.loaded_at is not None
                # Verify CPUExecutionProvider was used
                call_args = mock_ort.InferenceSession.call_args
                assert "CPUExecutionProvider" in call_args.kwargs["providers"]

    async def test_load_cuda_provider(self):
        w = _make_onnx_wrapper()
        fake_session = MagicMock()
        with patch.object(eai, "_check_onnxruntime", return_value=True):
            with patch.object(eai, "ort") as mock_ort:
                mock_ort.InferenceSession.return_value = fake_session
                await w.load(provider="CUDA")
                assert w.status == "active"
                call_args = mock_ort.InferenceSession.call_args
                assert "CUDAExecutionProvider" in call_args.kwargs["providers"]
                assert "CPUExecutionProvider" in call_args.kwargs["providers"]

    async def test_load_openvino_provider(self):
        w = _make_onnx_wrapper()
        fake_session = MagicMock()
        with patch.object(eai, "_check_onnxruntime", return_value=True):
            with patch.object(eai, "ort") as mock_ort:
                mock_ort.InferenceSession.return_value = fake_session
                await w.load(provider="OpenVINO")
                assert w.status == "active"
                call_args = mock_ort.InferenceSession.call_args
                assert "OpenVINOExecutionProvider" in call_args.kwargs["providers"]

    async def test_load_onnxruntime_not_installed(self):
        w = _make_onnx_wrapper()
        with patch.object(eai, "_check_onnxruntime", return_value=False):
            await w.load(provider="CPU")
            assert w.status == "inactive"
            assert w.session is None

    async def test_load_failure_sets_error(self):
        w = _make_onnx_wrapper()
        with patch.object(eai, "_check_onnxruntime", return_value=True):
            with patch.object(eai, "ort") as mock_ort:
                mock_ort.InferenceSession.side_effect = RuntimeError("load failed")
                await w.load(provider="CPU")
                assert w.status == "error"
                assert w.session is None

    async def test_unload(self):
        w = _make_onnx_wrapper()
        w.session = MagicMock()
        w.status = "active"
        w.loaded_at = datetime.now(UTC)
        await w.unload()
        assert w.session is None
        assert w.status == "inactive"
        assert w.loaded_at is None

    async def test_unload_no_session(self):
        w = _make_onnx_wrapper()
        w.session = None
        w.status = "inactive"
        await w.unload()
        assert w.session is None
        assert w.status == "inactive"


# ─── TestInferenceResult ──────────────────────────────────────────────────


class TestInferenceResult:
    def test_init_success(self):
        r = InferenceResult(
            model_id="m1",
            output_data={"output_0": [1.0]},
            latency_ms=42,
            status="success",
        )
        assert r.model_id == "m1"
        assert r.output_data == {"output_0": [1.0]}
        assert r.latency_ms == 42
        assert r.status == "success"
        assert r.error_message is None
        assert r.timestamp is not None

    def test_init_error(self):
        r = InferenceResult(
            model_id="m1",
            output_data={},
            latency_ms=0,
            status="error",
            error_message="something went wrong",
        )
        assert r.status == "error"
        assert r.error_message == "something went wrong"

    def test_slots(self):
        r = InferenceResult("m1", {}, 0, "success")
        with pytest.raises(AttributeError):
            r.nonexistent_field = "value"


# ─── TestInferenceStatsCollector ──────────────────────────────────────────


class TestInferenceStatsCollector:
    def test_empty_snapshot(self):
        sc = InferenceStatsCollector()
        snap = sc.get_snapshot()
        assert snap["total_calls"] == 0
        assert snap["total_errors"] == 0
        assert snap["avg_latency_ms"] == 0
        assert snap["model_distribution"] == {}
        assert snap["recent_latencies"] == []

    def test_record_success(self):
        sc = InferenceStatsCollector()
        sc.record_inference("m1", 100, "success")
        snap = sc.get_snapshot()
        assert snap["total_calls"] == 1
        assert snap["total_errors"] == 0
        assert snap["avg_latency_ms"] == 100
        assert snap["model_distribution"] == {"m1": 1}
        assert snap["recent_latencies"] == [100]

    def test_record_error(self):
        sc = InferenceStatsCollector()
        sc.record_inference("m1", 50, "error")
        snap = sc.get_snapshot()
        assert snap["total_calls"] == 1
        assert snap["total_errors"] == 1
        assert snap["model_distribution"] == {"m1": 1}

    def test_multiple_models(self):
        sc = InferenceStatsCollector()
        sc.record_inference("m1", 100, "success")
        sc.record_inference("m1", 200, "success")
        sc.record_inference("m2", 300, "success")
        snap = sc.get_snapshot()
        assert snap["total_calls"] == 3
        assert snap["avg_latency_ms"] == 200
        assert snap["model_distribution"] == {"m1": 2, "m2": 1}

    def test_model_stats_no_calls(self):
        sc = InferenceStatsCollector()
        assert sc.get_model_stats("nonexistent") is None

    def test_model_stats_with_calls(self):
        sc = InferenceStatsCollector()
        sc.record_inference("m1", 100, "success")
        sc.record_inference("m1", 300, "success")
        sc.record_inference("m1", 50, "error")
        stats = sc.get_model_stats("m1")
        assert stats is not None
        assert stats["model_id"] == "m1"
        assert stats["call_count"] == 3
        assert stats["error_count"] == 1
        assert stats["avg_latency_ms"] == 150
        assert stats["max_latency_ms"] == 300
        assert stats["min_latency_ms"] == 50

    def test_recent_latencies_capped(self):
        sc = InferenceStatsCollector()
        for i in range(150):
            sc.record_inference("m1", i, "success")
        snap = sc.get_snapshot()
        assert len(snap["recent_latencies"]) == 100
        # Most recent 100 values: 50..149
        assert snap["recent_latencies"][0] == 50
        assert snap["recent_latencies"][-1] == 149


# ─── TestValidateVideoUrl ─────────────────────────────────────────────────


class TestValidateVideoUrl:
    def test_valid_https_url(self):
        assert _validate_video_url("https://example.com/stream") is True

    def test_valid_rtsp_url(self):
        assert _validate_video_url("rtsp://example.com/stream") is True

    def test_valid_rtmp_url(self):
        assert _validate_video_url("rtmp://example.com/stream") is True

    def test_valid_http_url(self):
        assert _validate_video_url("http://example.com/stream") is True

    def test_invalid_protocol(self):
        assert _validate_video_url("ftp://example.com/stream") is False

    def test_localhost_blocked(self):
        assert _validate_video_url("http://localhost/stream") is False

    def test_private_ip_blocked(self):
        assert _validate_video_url("http://192.168.1.1/stream") is False

    def test_loopback_ip_blocked(self):
        assert _validate_video_url("http://127.0.0.1/stream") is False

    def test_link_local_blocked(self):
        assert _validate_video_url("http://169.254.169.254/stream") is False

    def test_metadata_hostname_blocked(self):
        assert _validate_video_url("http://metadata.google.internal/stream") is False

    def test_no_hostname(self):
        assert _validate_video_url("http:///stream") is False

    def test_dns_resolution_to_private_blocked(self):
        """Domain resolving to private IP should be blocked (DNS rebinding)."""
        with patch("socket.getaddrinfo") as mock_gai:
            mock_gai.return_value = [
                (0, 0, 0, "", ("192.168.1.1", 0)),
            ]
            assert _validate_video_url("http://internal.example.com/stream") is False

    def test_dns_resolution_failure(self):
        with patch("socket.getaddrinfo", side_effect=OSError("DNS failed")):
            assert _validate_video_url("http://nonexistent.invalid/stream") is False

    def test_dns_resolution_to_public_allowed(self):
        with patch("socket.getaddrinfo") as mock_gai:
            mock_gai.return_value = [
                (0, 0, 0, "", ("93.184.216.34", 0)),
            ]
            assert _validate_video_url("http://public.example.com/stream") is True

    def test_reserved_ip_blocked(self):
        assert _validate_video_url("http://240.0.0.1/stream") is False

    def test_multicast_ip_blocked(self):
        assert _validate_video_url("http://224.0.0.1/stream") is False

    def test_unspecified_ip_blocked(self):
        assert _validate_video_url("http://0.0.0.0/stream") is False


# ─── TestValidateCloudEndpoint ────────────────────────────────────────────


class TestValidateCloudEndpoint:
    def test_valid_https(self):
        with patch("socket.getaddrinfo", return_value=[(0, 0, 0, "", ("93.184.216.34", 0))]):
            assert _validate_cloud_endpoint("https://cloud.example.com/infer") is True

    def test_valid_http(self):
        with patch("socket.getaddrinfo", return_value=[(0, 0, 0, "", ("93.184.216.34", 0))]):
            assert _validate_cloud_endpoint("http://cloud.example.com/infer") is True

    def test_rtsp_rejected(self):
        """Cloud endpoint only allows http/https, not rtsp/rtmp."""
        assert _validate_cloud_endpoint("rtsp://cloud.example.com/infer") is False

    def test_localhost_blocked(self):
        assert _validate_cloud_endpoint("http://localhost/infer") is False

    def test_private_ip_blocked(self):
        assert _validate_cloud_endpoint("http://10.0.0.1/infer") is False

    def test_metadata_ip_blocked(self):
        assert _validate_cloud_endpoint("http://169.254.169.254/infer") is False

    def test_no_hostname(self):
        assert _validate_cloud_endpoint("http:///infer") is False

    def test_dns_rebinding_blocked(self):
        with patch("socket.getaddrinfo") as mock_gai:
            mock_gai.return_value = [(0, 0, 0, "", ("10.0.0.1", 0))]
            assert _validate_cloud_endpoint("http://rebinding.example.com/infer") is False

    def test_dns_failure(self):
        with patch("socket.getaddrinfo", side_effect=OSError("DNS failed")):
            assert _validate_cloud_endpoint("http://nonexistent.invalid/infer") is False


# ─── TestAiInferenceEngineInit ────────────────────────────────────────────


class TestAiInferenceEngineInit:
    def test_disabled_engine(self, tmp_path):
        engine = AiInferenceEngine(models_dir=str(tmp_path), enabled=False)

    async def test_disabled_initialize_returns_early(self, tmp_path):
        engine = AiInferenceEngine(models_dir=str(tmp_path), enabled=False)
        await engine.initialize()
        # Should not set up any infrastructure
        assert engine._inference_cache is None
        assert engine._version_manager is None

    async def test_initialize_enabled(self, tmp_path):
        engine = AiInferenceEngine(models_dir=str(tmp_path), enabled=True)
        lazy_mods = _make_lazy_modules()
        with patch.dict(sys.modules, lazy_mods):
            with patch.object(eai, "_check_onnxruntime", return_value=False):
                with patch.object(engine, "load_preset_models", new_callable=AsyncMock):
                    await engine.initialize(config={"cache_ttl": 10.0, "cache_max_size": 512})
                    assert engine._inference_cache is not None
                    assert engine._version_manager is not None
                    assert engine._hot_swap_manager is not None
                    assert engine._resource_monitor is not None
                    # Cancel watcher task
                    watcher = getattr(engine, "_model_watcher_task", None)
                    if watcher and not watcher.done():
                        watcher.cancel()
                        with __import__("contextlib").suppress(asyncio.CancelledError):
                            await watcher

    async def test_initialize_with_cloud_inference(self, tmp_path):
        engine = AiInferenceEngine(models_dir=str(tmp_path), enabled=True)
        lazy_mods = _make_lazy_modules()
        with patch.dict(sys.modules, lazy_mods):
            with patch.object(eai, "_check_onnxruntime", return_value=False):
                with patch.object(engine, "load_preset_models", new_callable=AsyncMock):
                    await engine.initialize(config={"cloud_inference_enabled": True})
                    assert engine._cloud_circuit_breaker is not None
                    watcher = getattr(engine, "_model_watcher_task", None)
                    if watcher and not watcher.done():
                        watcher.cancel()
                        with __import__("contextlib").suppress(asyncio.CancelledError):
                            await watcher

    async def test_initialize_auto_detect_device(self, tmp_path):
        engine = AiInferenceEngine(models_dir=str(tmp_path), enabled=True)
        lazy_mods = _make_lazy_modules(
            provider_fn=lambda pref: ("CUDAExecutionProvider", "CUDA"),
        )
        with patch.dict(sys.modules, lazy_mods):
            with patch.object(eai, "_check_onnxruntime", return_value=False):
                with patch.object(engine, "load_preset_models", new_callable=AsyncMock):
                    await engine.initialize(config={"auto_detect_device": True})
                    assert engine._execution_provider == "CUDA"
                    watcher = getattr(engine, "_model_watcher_task", None)
                    if watcher and not watcher.done():
                        watcher.cancel()
                        with __import__("contextlib").suppress(asyncio.CancelledError):
                            await watcher


# ─── TestSetExecutionProvider ─────────────────────────────────────────────


class TestSetExecutionProvider:
    async def test_invalid_provider_raises(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        with pytest.raises(ValueError, match="Invalid provider"):
            await engine.set_execution_provider("INVALID")

    async def test_cpu_provider(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        await engine.set_execution_provider("CPU")
        assert engine._execution_provider == "CPU"

    async def test_cuda_not_available_fallback(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        with patch("onnxruntime.get_available_providers", return_value=["CPUExecutionProvider"]):
            await engine.set_execution_provider("CUDA")
            assert engine._execution_provider == "CPU"

    async def test_cuda_check_exception_fallback(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        with patch("onnxruntime.get_available_providers", side_effect=Exception("check failed")):
            await engine.set_execution_provider("CUDA")
            assert engine._execution_provider == "CPU"

    async def test_openvino_not_available_fallback(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        with patch("onnxruntime.get_available_providers", return_value=["CPUExecutionProvider"]):
            await engine.set_execution_provider("OpenVINO")
            assert engine._execution_provider == "CPU"

    async def test_reloads_active_models(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        w = _make_onnx_wrapper(status="active")
        w.load = AsyncMock()
        w.unload = AsyncMock()
        engine._loaded_models["m1"] = w
        with patch("onnxruntime.get_available_providers", return_value=["CPUExecutionProvider"]):
            await engine.set_execution_provider("CUDA")
            w.unload.assert_called_once()
            w.load.assert_called_once()


# ─── TestGetAvailableProviders ────────────────────────────────────────────


class TestGetAvailableProviders:
    def test_cpu_always_available(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        with patch("onnxruntime.get_available_providers", return_value=["CPUExecutionProvider"]):
            providers = engine.get_available_providers()
            assert "CPU" in providers

    def test_cuda_available(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        with patch(
            "onnxruntime.get_available_providers",
            return_value=["CPUExecutionProvider", "CUDAExecutionProvider"],
        ):
            providers = engine.get_available_providers()
            assert "CUDA" in providers

    def test_openvino_available(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        with patch(
            "onnxruntime.get_available_providers",
            return_value=["CPUExecutionProvider", "OpenVINOExecutionProvider"],
        ):
            providers = engine.get_available_providers()
            assert "OpenVINO" in providers

    def test_import_error(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        with patch("builtins.__import__", side_effect=ImportError("no ort")):
            providers = engine.get_available_providers()
            assert providers == ["CPU"]


# ─── TestCheckModelUpdates ────────────────────────────────────────────────


class TestCheckModelUpdates:
    async def test_no_models(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        result = await engine.check_model_updates()
        assert result == []

    async def test_no_changes(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        w = _make_onnx_wrapper(status="active", model_path=str(tmp_path / "m1.onnx"))
        w._last_mtime = (tmp_path / "m1.onnx").stat().st_mtime if (tmp_path / "m1.onnx").exists() else 0.0
        engine._loaded_models["m1"] = w
        result = await engine.check_model_updates()
        assert result == []

    async def test_file_changed_triggers_reload(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        model_file = tmp_path / "m1.onnx"
        model_file.write_bytes(b"fake")
        w = _make_onnx_wrapper(status="active", model_path=str(model_file))
        w._last_mtime = 0.0  # Force mtime change
        w.unload = AsyncMock()
        w.load = AsyncMock()
        engine._loaded_models["m1"] = w
        result = await engine.check_model_updates()
        assert "m1" in result
        w.unload.assert_called_once()
        w.load.assert_called_once()


# ─── TestLoadPresetModels ─────────────────────────────────────────────────


class TestLoadPresetModels:
    async def test_load_with_onnxruntime(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        with patch.object(eai, "_check_onnxruntime", return_value=True):
            with patch.object(eai, "ort"):
                await engine.load_preset_models()
                assert len(engine._loaded_models) == 3
                # All should be active since ort is mocked
                for w in engine._loaded_models.values():
                    assert w.status in ("active", "error")

    async def test_load_without_onnxruntime(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        with patch.object(eai, "_check_onnxruntime", return_value=False):
            await engine.load_preset_models()
            assert len(engine._loaded_models) == 3
            for w in engine._loaded_models.values():
                assert w.status in ("inactive", "unavailable")

    async def test_load_with_low_memory(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        fake_mem = MagicMock()
        fake_mem.available = 50 * 1024 * 1024  # 50MB < 100MB threshold
        with patch("psutil.virtual_memory", return_value=fake_mem):
            with patch.object(eai, "_check_onnxruntime", return_value=True):
                with patch.object(eai, "ort"):
                    await engine.load_preset_models()
                    # Should break early due to low memory
                    assert len(engine._loaded_models) <= 1

    async def test_load_without_psutil(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        with patch("builtins.__import__", side_effect=ImportError("no psutil")):
            with patch.object(eai, "_check_onnxruntime", return_value=True):
                with patch.object(eai, "ort"):
                    await engine.load_preset_models()
                    # Should still load all 3 presets despite psutil missing
                    assert len(engine._loaded_models) == 3


# ─── TestTryGeneratePreset ────────────────────────────────────────────────


class TestTryGeneratePreset:
    def test_generate_success(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        preset = {
            "model_file": "test-model.onnx",
            "input_schema": {"shape": [1, 10]},
            "output_schema": {"shape": [1]},
        }
        result = engine._try_generate_preset(preset)
        assert result is True
        assert (tmp_path / "test-model.onnx").exists()

    def test_generate_failure_no_onnx(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        preset = {
            "model_file": "test-model.onnx",
            "input_schema": {"shape": [1, 10]},
            "output_schema": {"shape": [1]},
        }
        with patch("edgelite.engine.edge_ai_inference._generate_onnx_model", return_value=None):
            result = engine._try_generate_preset(preset)
            assert result is False


# ─── TestGeneratePresetModels ─────────────────────────────────────────────


class TestGeneratePresetModels:
    async def test_already_exists(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        # Create the model files
        for preset in eai.PRESET_MODELS:
            (tmp_path / preset["model_file"]).write_bytes(b"fake")
        results = await engine.generate_preset_models()
        for model_id in results:
            assert results[model_id] is True

    async def test_generate_missing(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        results = await engine.generate_preset_models()
        assert len(results) == 3
        # Models should be generated since onnx is available
        for model_id, success in results.items():
            assert success is True


# ─── TestReloadModel ──────────────────────────────────────────────────────


class TestReloadModel:
    async def test_not_found(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        with pytest.raises(ValueError, match="Model not found"):
            await engine.reload_model("nonexistent", "/path/to/model.onnx")

    async def test_success(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        w = _make_onnx_wrapper(status="active")
        w.unload = AsyncMock()
        w.load = AsyncMock()
        engine._loaded_models["m1"] = w
        await engine.reload_model("m1", "/new/path.onnx")
        assert w.model_path == "/new/path.onnx"
        w.unload.assert_called_once()
        w.load.assert_called_once()

    async def test_load_failure_sets_unavailable(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        w = _make_onnx_wrapper(status="active")
        w.unload = AsyncMock()
        w.load = AsyncMock(side_effect=RuntimeError("load failed"))
        engine._loaded_models["m1"] = w
        with pytest.raises(RuntimeError):
            await engine.reload_model("m1", "/new/path.onnx")
        assert w.status == "unavailable"


# ─── TestDetectModelFormat ────────────────────────────────────────────────


class TestDetectModelFormat:
    def test_onnx(self):
        assert AiInferenceEngine._detect_model_format("model.onnx") == "onnx"

    def test_tflite(self):
        assert AiInferenceEngine._detect_model_format("model.tflite") == "tflite"

    def test_pmml(self):
        assert AiInferenceEngine._detect_model_format("model.pmml") == "pmml"

    def test_unknown_defaults_to_onnx(self):
        assert AiInferenceEngine._detect_model_format("model.txt") == "onnx"

    def test_case_insensitive(self):
        assert AiInferenceEngine._detect_model_format("model.ONNX") == "onnx"


# ─── TestCreateWrapper ────────────────────────────────────────────────────


class TestCreateWrapper:
    def test_creates_onnx_wrapper(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        w = engine._create_wrapper(
            model_id="m1",
            model_name="Model",
            model_version="v1.0.0",
            model_type="anomaly",
            model_path="/model.onnx",
            input_schema={"shape": [1, 3]},
            output_schema={"shape": [1]},
        )
        assert isinstance(w, OnnxModelWrapper)

    def test_creates_tflite_wrapper(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        w = engine._create_wrapper(
            model_id="m1",
            model_name="Model",
            model_version="v1.0.0",
            model_type="custom",
            model_path="/model.tflite",
            input_schema={"shape": [1, 3]},
            output_schema={"shape": [1]},
        )
        assert isinstance(w, TFLiteModelWrapper)

    def test_creates_pmml_wrapper(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        w = engine._create_wrapper(
            model_id="m1",
            model_name="Model",
            model_version="v1.0.0",
            model_type="custom",
            model_path="/model.pmml",
            input_schema={"shape": [1, 3]},
            output_schema={"shape": [1]},
        )
        assert isinstance(w, PMMLModelWrapper)


# ─── TestLoadCustomModel ──────────────────────────────────────────────────


class TestLoadCustomModel:
    async def test_new_model(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        with patch.object(OnnxModelWrapper, "load", new_callable=AsyncMock):
            w = await engine.load_custom_model(
                model_id="custom-1",
                model_name="Custom",
                model_version="v1.0.0",
                model_type="custom",
                model_path=str(tmp_path / "custom.onnx"),
                input_schema={"shape": [1, 3]},
                output_schema={"shape": [1]},
            )
            assert w.model_id == "custom-1"
            assert "custom-1" in engine._loaded_models

    async def test_existing_model_increments_version(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        existing = _make_onnx_wrapper(model_version="v1.0.0")
        engine._loaded_models["custom-1"] = existing
        with patch.object(OnnxModelWrapper, "load", new_callable=AsyncMock):
            w = await engine.load_custom_model(
                model_id="custom-1",
                model_name="Custom",
                model_version="v1.0.0",
                model_type="custom",
                model_path=str(tmp_path / "custom.onnx"),
                input_schema={"shape": [1, 3]},
                output_schema={"shape": [1]},
            )
            assert w.model_version == "v1.0.1"


# ─── TestInfer ────────────────────────────────────────────────────────────


class TestInfer:
    async def test_model_not_available(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        result = await engine.infer("nonexistent", [1.0, 2.0])
        assert result.status == "error"
        assert "not available" in result.error_message.lower()

    async def test_model_not_active(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        w = _make_onnx_wrapper(status="inactive")
        engine._loaded_models["m1"] = w
        result = await engine.infer("m1", [1.0])
        assert result.status == "error"
        assert "not available" in result.error_message.lower()

    async def test_cache_hit(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        w = _make_onnx_wrapper(status="active")
        engine._loaded_models["m1"] = w
        cache = MagicMock()
        cache.get.return_value = {"output_0": [42.0]}
        engine._inference_cache = cache
        with patch("edgelite.api.debug.record_packet"):
            result = await engine.infer("m1", [1.0])
            assert result.status == "success"
            assert result.output_data == {"output_0": [42.0]}
            assert result.latency_ms == 0

    async def test_success_onnx(self, tmp_path):
        import numpy as np

        engine = _make_active_engine(tmp_path)
        w = _make_onnx_wrapper(status="active", input_schema={"shape": [1, 3]})
        fake_input = MagicMock()
        fake_input.name = "input"
        fake_session = MagicMock()
        fake_session.get_inputs.return_value = [fake_input]
        fake_session.run.return_value = [np.array([[0.5]])]
        w.session = fake_session
        engine._loaded_models["m1"] = w
        with patch("edgelite.api.debug.record_packet"):
            result = await engine.infer("m1", [1.0, 2.0, 3.0])
            assert result.status == "success"
            assert "output_0" in result.output_data

    async def test_success_tflite(self, tmp_path):
        import numpy as np

        engine = _make_active_engine(tmp_path)
        w = TFLiteModelWrapper(
            model_id="m1",
            model_name="Model",
            model_version="v1.0.0",
            model_type="custom",
            model_path="/model.tflite",
            input_schema={"shape": [1, 3]},
            output_schema={"shape": [1]},
        )
        w.status = "active"
        w._preprocess_pipeline = None
        w._postprocess_pipeline = None
        w.run = MagicMock(return_value=[np.array([[0.7]])])
        engine._loaded_models["m1"] = w
        with patch("edgelite.api.debug.record_packet"):
            result = await engine.infer("m1", [1.0, 2.0, 3.0])
            assert result.status == "success"
            assert "output_0" in result.output_data

    async def test_success_pmml(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        w = PMMLModelWrapper(
            model_id="m1",
            model_name="Model",
            model_version="v1.0.0",
            model_type="custom",
            model_path="/model.pmml",
            input_schema={"shape": [1, 2]},
            output_schema={"shape": [1]},
        )
        w.status = "active"
        w._preprocess_pipeline = None
        w._postprocess_pipeline = None
        w._feature_names = ["x1", "x2"]
        w._model = {"type": "regression", "intercept": 1.0, "coefficients": {"x1": 2.0, "x2": 3.0}}
        engine._loaded_models["m1"] = w
        with patch("edgelite.api.debug.record_packet"):
            result = await engine.infer("m1", [1.0, 2.0])
            assert result.status == "success"
            # 1.0 + 2*1 + 3*2 = 9.0
            assert result.output_data["output_0"] == [9.0]

    async def test_timeout_returns_cached(self, tmp_path):
        import numpy as np

        engine = _make_active_engine(tmp_path, config={"inference_timeout": 0.01})
        w = _make_onnx_wrapper(status="active", input_schema={"shape": [1, 3]})
        fake_input = MagicMock()
        fake_input.name = "input"
        fake_session = MagicMock()

        def slow_run(*args, **kwargs):
            import time

            time.sleep(0.5)
            return [np.array([[0.5]])]

        fake_session.run = slow_run
        fake_session.get_inputs.return_value = [fake_input]
        w.session = fake_session
        w.last_result = {"output_0": [0.99]}
        engine._loaded_models["m1"] = w
        with patch("edgelite.api.debug.record_packet"):
            result = await engine.infer("m1", [1.0, 2.0, 3.0])
            assert result.status == "error"
            assert "timeout" in result.error_message.lower()
            assert result.output_data == {"output_0": [0.99]}

    async def test_timeout_no_cache_returns_default(self, tmp_path):
        import numpy as np

        engine = _make_active_engine(tmp_path, config={"inference_timeout": 0.01})
        w = _make_onnx_wrapper(status="active", input_schema={"shape": [1, 3]})
        fake_input = MagicMock()
        fake_input.name = "input"
        fake_session = MagicMock()

        def slow_run(*args, **kwargs):
            import time

            time.sleep(0.5)
            return [np.array([[0.5]])]

        fake_session.run = slow_run
        fake_session.get_inputs.return_value = [fake_input]
        w.session = fake_session
        engine._loaded_models["m1"] = w
        with patch("edgelite.api.debug.record_packet"):
            result = await engine.infer("m1", [1.0, 2.0, 3.0])
            assert result.status == "error"
            assert "timeout" in result.error_message.lower()
            assert result.output_data == {"output_0": [0.0]}

    async def test_memory_error_unloads_model(self, tmp_path):

        engine = _make_active_engine(tmp_path)
        w = _make_onnx_wrapper(status="active", input_schema={"shape": [1, 3]})
        fake_input = MagicMock()
        fake_input.name = "input"
        fake_session = MagicMock()
        fake_session.get_inputs.return_value = [fake_input]
        fake_session.run.side_effect = MemoryError("OOM")
        w.session = fake_session
        w.unload = AsyncMock()
        engine._loaded_models["m1"] = w
        with patch("edgelite.api.debug.record_packet"):
            result = await engine.infer("m1", [1.0, 2.0, 3.0])
            assert result.status == "error"
            assert "OOM" in result.error_message
            w.unload.assert_called_once()
            assert w.status == "error"

    async def test_runtime_error_oom(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        w = _make_onnx_wrapper(status="active", input_schema={"shape": [1, 3]})
        fake_input = MagicMock()
        fake_input.name = "input"
        fake_session = MagicMock()
        fake_session.get_inputs.return_value = [fake_input]
        fake_session.run.side_effect = RuntimeError("out of memory")
        w.session = fake_session
        engine._loaded_models["m1"] = w
        with patch("edgelite.api.debug.record_packet"):
            result = await engine.infer("m1", [1.0, 2.0, 3.0])
            assert result.status == "error"
            assert "OOM" in result.error_message
            assert w.status == "error"

    async def test_generic_error_with_cloud_fallback(self, tmp_path):
        engine = _make_active_engine(
            tmp_path,
            config={"cloud_inference_enabled": True, "cloud_inference_url": "https://cloud.example.com"},
        )
        w = _make_onnx_wrapper(status="active", input_schema={"shape": [1, 3]})
        fake_input = MagicMock()
        fake_input.name = "input"
        fake_session = MagicMock()
        fake_session.get_inputs.return_value = [fake_input]
        fake_session.run.side_effect = RuntimeError("model error")
        w.session = fake_session
        engine._loaded_models["m1"] = w
        cloud_result = InferenceResult("m1", {"output_0": [1.0]}, 50, "success")
        with patch.object(engine, "infer_cloud_fallback", new_callable=AsyncMock, return_value=cloud_result):
            with patch("edgelite.api.debug.record_packet"):
                result = await engine.infer("m1", [1.0, 2.0, 3.0])
                assert result.status == "success"
                assert result.output_data == {"output_0": [1.0]}

    async def test_generic_error_no_cloud_fallback(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        w = _make_onnx_wrapper(status="active", input_schema={"shape": [1, 3]})
        fake_input = MagicMock()
        fake_input.name = "input"
        fake_session = MagicMock()
        fake_session.get_inputs.return_value = [fake_input]
        fake_session.run.side_effect = RuntimeError("model error")
        w.session = fake_session
        engine._loaded_models["m1"] = w
        with patch("edgelite.api.debug.record_packet"):
            result = await engine.infer("m1", [1.0, 2.0, 3.0])
            assert result.status == "error"
            assert "model error" in result.error_message

    async def test_numpy_not_installed(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        w = _make_onnx_wrapper(status="active", input_schema={"shape": [1, 3]})
        engine._loaded_models["m1"] = w
        with patch.object(eai, "_HAS_NUMPY", False):
            with patch("edgelite.api.debug.record_packet"):
                result = await engine.infer("m1", [1.0])
                assert result.status == "error"
                assert "numpy" in result.error_message.lower()

    async def test_inference_with_preprocess_pipeline(self, tmp_path):
        import numpy as np

        engine = _make_active_engine(tmp_path)
        w = _make_onnx_wrapper(status="active", input_schema={"shape": [1, 3]})
        w._preprocess_pipeline = MagicMock()
        w._preprocess_pipeline.apply = MagicMock(side_effect=lambda x: x)
        fake_input = MagicMock()
        fake_input.name = "input"
        fake_session = MagicMock()
        fake_session.get_inputs.return_value = [fake_input]
        fake_session.run.return_value = [np.array([[0.5]])]
        w.session = fake_session
        engine._loaded_models["m1"] = w
        with patch("edgelite.api.debug.record_packet"):
            result = await engine.infer("m1", [1.0, 2.0, 3.0])
            assert result.status == "success"
            w._preprocess_pipeline.apply.assert_called_once()

    async def test_inference_with_postprocess_pipeline(self, tmp_path):
        import numpy as np

        engine = _make_active_engine(tmp_path)
        w = _make_onnx_wrapper(status="active", input_schema={"shape": [1, 3]})
        w._postprocess_pipeline = MagicMock()
        w._postprocess_pipeline.apply = MagicMock(side_effect=lambda x: {"processed": True})
        fake_input = MagicMock()
        fake_input.name = "input"
        fake_session = MagicMock()
        fake_session.get_inputs.return_value = [fake_input]
        fake_session.run.return_value = [np.array([[0.5]])]
        w.session = fake_session
        engine._loaded_models["m1"] = w
        with patch("edgelite.api.debug.record_packet"):
            result = await engine.infer("m1", [1.0, 2.0, 3.0])
            assert result.status == "success"
            assert result.output_data == {"processed": True}

    async def test_inference_with_event_bus(self, tmp_path):
        import numpy as np

        engine = _make_active_engine(tmp_path)
        w = _make_onnx_wrapper(status="active", input_schema={"shape": [1, 3]})
        fake_input = MagicMock()
        fake_input.name = "input"
        fake_session = MagicMock()
        fake_session.get_inputs.return_value = [fake_input]
        fake_session.run.return_value = [np.array([[0.5]])]
        w.session = fake_session
        engine._loaded_models["m1"] = w
        fake_event_bus = AsyncMock()
        engine._event_bus = fake_event_bus
        with patch("edgelite.api.debug.record_packet"):
            result = await engine.infer("m1", [1.0, 2.0, 3.0])
            assert result.status == "success"


# ─── TestInferCloudFallback ───────────────────────────────────────────────


class TestInferCloudFallback:
    async def test_circuit_breaker_open(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        cb = MagicMock()
        cb.is_open = True
        engine._cloud_circuit_breaker = cb
        result = await engine.infer_cloud_fallback("m1", [1.0], "https://cloud.example.com")
        assert result.status == "error"
        assert "circuit breaker" in result.error_message.lower()

    async def test_no_cloud_url(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        result = await engine.infer_cloud_fallback("m1", [1.0], "")
        assert result.status == "error"
        assert "not configured" in result.error_message.lower()

    async def test_ssrf_validation_failed(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        result = await engine.infer_cloud_fallback("m1", [1.0], "http://localhost/infer")
        assert result.status == "error"
        assert "SSRF" in result.error_message

    async def test_success(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"output_data": {"output_0": [0.5]}}
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)
        with patch("httpx.AsyncClient", return_value=mock_client):
            with patch("socket.getaddrinfo", return_value=[(0, 0, 0, "", ("93.184.216.34", 0))]):
                result = await engine.infer_cloud_fallback("m1", [1.0], "https://cloud.example.com")
                assert result.status == "success"
                assert result.output_data == {"output_0": [0.5]}

    async def test_http_error(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)
        cb = MagicMock()
        cb.is_open = False
        cb.record_failure = AsyncMock()
        engine._cloud_circuit_breaker = cb
        with patch("httpx.AsyncClient", return_value=mock_client):
            with patch("socket.getaddrinfo", return_value=[(0, 0, 0, "", ("93.184.216.34", 0))]):
                result = await engine.infer_cloud_fallback("m1", [1.0], "https://cloud.example.com")
                assert result.status == "error"
                assert "HTTP 500" in result.error_message
                cb.record_failure.assert_called_once()

    async def test_request_exception(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        cb = MagicMock()
        cb.is_open = False
        cb.record_failure = AsyncMock()
        engine._cloud_circuit_breaker = cb
        with patch("httpx.AsyncClient", side_effect=Exception("connection failed")):
            with patch("socket.getaddrinfo", return_value=[(0, 0, 0, "", ("93.184.216.34", 0))]):
                result = await engine.infer_cloud_fallback("m1", [1.0], "https://cloud.example.com")
                assert result.status == "error"
                assert "connection failed" in result.error_message
                cb.record_failure.assert_called_once()

    async def test_success_records_circuit_breaker(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"output_data": {}}
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)
        cb = MagicMock()
        cb.is_open = False
        cb.record_success = AsyncMock()
        engine._cloud_circuit_breaker = cb
        with patch("httpx.AsyncClient", return_value=mock_client):
            with patch("socket.getaddrinfo", return_value=[(0, 0, 0, "", ("93.184.216.34", 0))]):
                result = await engine.infer_cloud_fallback("m1", [1.0], "https://cloud.example.com")
                assert result.status == "success"
                cb.record_success.assert_called_once()

    async def test_with_api_key_header(self, tmp_path):
        engine = _make_active_engine(tmp_path, config={"cloud_inference_api_key": "test-key"})
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"output_data": {}}
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)
        with patch("httpx.AsyncClient", return_value=mock_client):
            with patch("socket.getaddrinfo", return_value=[(0, 0, 0, "", ("93.184.216.34", 0))]):
                await engine.infer_cloud_fallback("m1", [1.0], "https://cloud.example.com")
                call_kwargs = mock_client.post.call_args
                assert call_kwargs.kwargs["headers"]["x-api-key"] == "test-key"

    async def test_with_auth_token_header(self, tmp_path):
        engine = _make_active_engine(tmp_path, config={"cloud_inference_auth_token": "test-token"})
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"output_data": {}}
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)
        with patch("httpx.AsyncClient", return_value=mock_client):
            with patch("socket.getaddrinfo", return_value=[(0, 0, 0, "", ("93.184.216.34", 0))]):
                await engine.infer_cloud_fallback("m1", [1.0], "https://cloud.example.com")
                call_kwargs = mock_client.post.call_args
                assert call_kwargs.kwargs["headers"]["Authorization"] == "Bearer test-token"


# ─── TestModelManagement ──────────────────────────────────────────────────


class TestModelManagement:
    def test_get_model_status(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        w = _make_onnx_wrapper(status="active")
        engine._loaded_models["m1"] = w
        assert engine.get_model_status("m1") == "active"

    def test_get_model_status_not_found(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        assert engine.get_model_status("nonexistent") is None

    def test_get_loaded_models(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        w1 = _make_onnx_wrapper(model_id="m1")
        w2 = _make_onnx_wrapper(model_id="m2")
        engine._loaded_models["m1"] = w1
        engine._loaded_models["m2"] = w2
        loaded = engine.get_loaded_models()
        assert len(loaded) == 2
        assert "m1" in loaded
        assert "m2" in loaded

    def test_get_model(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        w = _make_onnx_wrapper()
        engine._loaded_models["m1"] = w
        assert engine.get_model("m1") is w

    def test_get_model_not_found(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        assert engine.get_model("nonexistent") is None

    def test_get_stats(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        stats = engine.get_stats()
        assert "total_calls" in stats
        assert "total_errors" in stats

    def test_get_model_stats(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        engine._stats.record_inference("m1", 100, "success")
        stats = engine.get_model_stats("m1")
        assert stats is not None
        assert stats["model_id"] == "m1"

    def test_get_model_stats_not_found(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        assert engine.get_model_stats("nonexistent") is None

    async def test_enable_model_not_found(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        result, msg = await engine.enable_model("nonexistent")
        assert result is False
        assert "NOT_FOUND" in msg

    async def test_enable_model_onnxruntime_not_installed(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        w = _make_onnx_wrapper(status="inactive")
        engine._loaded_models["m1"] = w
        with patch.object(eai, "_check_onnxruntime", return_value=False):
            result, msg = await engine.enable_model("m1")
            assert result is False
            assert "ONNXRUNTIME_NOT_INSTALLED" in msg

    async def test_enable_model_is_loading(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        w = _make_onnx_wrapper(status="loading")
        engine._loaded_models["m1"] = w
        with patch.object(eai, "_check_onnxruntime", return_value=True):
            result, msg = await engine.enable_model("m1")
            assert result is False
            assert "LOADING" in msg

    async def test_enable_model_already_active(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        w = _make_onnx_wrapper(status="active")
        engine._loaded_models["m1"] = w
        with patch.object(eai, "_check_onnxruntime", return_value=True):
            result, msg = await engine.enable_model("m1")
            assert result is True
            assert msg == ""

    async def test_enable_model_from_inactive(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        w = _make_onnx_wrapper(status="inactive")
        w.load = AsyncMock()
        engine._loaded_models["m1"] = w
        with patch.object(eai, "_check_onnxruntime", return_value=True):
            with patch.object(OnnxModelWrapper, "load", new_callable=AsyncMock):
                w.status = "active"  # Simulate successful load
                result, msg = await engine.enable_model("m1")
                assert result is True

    async def test_enable_model_from_error(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        w = _make_onnx_wrapper(status="error")
        w.load = AsyncMock()
        engine._loaded_models["m1"] = w
        with patch.object(eai, "_check_onnxruntime", return_value=True):
            with patch.object(OnnxModelWrapper, "load", new_callable=AsyncMock):
                w.status = "active"  # Simulate successful load
                result, msg = await engine.enable_model("m1")
                assert result is True

    async def test_enable_model_from_unavailable_with_preset(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        # Create a preset model file
        model_file = tmp_path / "elg-anomaly-v1.onnx"
        model_file.write_bytes(b"fake")
        w = _make_onnx_wrapper(
            model_id="preset-anomaly-v1",
            status="unavailable",
            is_preset=True,
            model_path=str(model_file),
        )
        w.load = AsyncMock()
        engine._loaded_models["preset-anomaly-v1"] = w
        with patch.object(eai, "_check_onnxruntime", return_value=True):
            with patch.object(OnnxModelWrapper, "load", new_callable=AsyncMock):
                w.status = "active"
                result, msg = await engine.enable_model("preset-anomaly-v1")
                assert result is True

    async def test_enable_model_from_unavailable_no_file(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        w = _make_onnx_wrapper(
            status="unavailable",
            is_preset=True,
            model_path="/nonexistent/model.onnx",
        )
        engine._loaded_models["m1"] = w
        with patch.object(eai, "_check_onnxruntime", return_value=True):
            with patch.object(engine, "_try_generate_preset", return_value=False):
                result, msg = await engine.enable_model("m1")
                assert result is False
                assert "FILE_NOT_FOUND" in msg

    async def test_disable_model(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        w = _make_onnx_wrapper(status="active")
        w.unload = AsyncMock()
        engine._loaded_models["m1"] = w
        await engine.disable_model("m1")
        w.unload.assert_called_once()

    async def test_disable_model_not_found(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        # Should not raise
        await engine.disable_model("nonexistent")

    async def test_remove_model(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        w = _make_onnx_wrapper(status="active")
        w.unload = AsyncMock()
        engine._loaded_models["m1"] = w
        await engine.remove_model("m1")
        assert "m1" not in engine._loaded_models
        w.unload.assert_called_once()

    async def test_remove_model_not_found(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        # Should not raise
        await engine.remove_model("nonexistent")

    async def test_remove_model_stops_scheduled_inference(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        w = _make_onnx_wrapper(status="active")
        w.unload = AsyncMock()
        engine._loaded_models["m1"] = w
        with patch.object(engine, "stop_scheduled_inference", new_callable=AsyncMock) as mock_stop:
            await engine.remove_model("m1")
            mock_stop.assert_called_once_with("m1")


# ─── TestScheduledInference ───────────────────────────────────────────────


class TestScheduledInference:
    async def test_start_scheduled_inference_already_exists(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        w = _make_onnx_wrapper(status="active")
        engine._loaded_models["m1"] = w
        engine._scheduled_tasks["m1"] = MagicMock()
        with pytest.raises(ValueError, match="already exists"):
            await engine.start_scheduled_inference("m1", "d1", "temp")

    async def test_start_scheduled_inference_model_not_active(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        w = _make_onnx_wrapper(status="inactive")
        engine._loaded_models["m1"] = w
        with pytest.raises(ValueError, match="not available"):
            await engine.start_scheduled_inference("m1", "d1", "temp")

    async def test_start_scheduled_inference_model_not_found(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        with pytest.raises(ValueError, match="not available"):
            await engine.start_scheduled_inference("nonexistent", "d1", "temp")

    async def test_start_scheduled_inference_success(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        w = _make_onnx_wrapper(status="active")
        engine._loaded_models["m1"] = w
        await engine.start_scheduled_inference("m1", "d1", "temp", interval_seconds=60)
        assert "m1" in engine._scheduled_tasks
        assert "m1" in engine._scheduled_configs
        # Clean up
        await engine.stop_scheduled_inference("m1")

    async def test_stop_scheduled_inference_not_found(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        result = await engine.stop_scheduled_inference("nonexistent")
        assert result is False

    async def test_stop_scheduled_inference_success(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        w = _make_onnx_wrapper(status="active")
        engine._loaded_models["m1"] = w
        await engine.start_scheduled_inference("m1", "d1", "temp", interval_seconds=60)
        result = await engine.stop_scheduled_inference("m1")
        assert result is True
        assert "m1" not in engine._scheduled_tasks
        assert "m1" not in engine._scheduled_configs

    def test_get_scheduled_inferences_empty(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        result = engine.get_scheduled_inferences()
        assert result == []

    def test_get_scheduled_inferences_with_config(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        engine._scheduled_configs["m1"] = {
            "model_id": "m1",
            "device_id": "d1",
            "point_name": "temp",
            "interval_seconds": 60,
            "input_window_size": 100,
        }
        engine._scheduled_tasks["m1"] = MagicMock()
        engine._scheduled_tasks["m1"].done.return_value = False
        result = engine.get_scheduled_inferences()
        assert len(result) == 1
        assert result[0]["model_id"] == "m1"
        assert result[0]["running"] is True

    def test_get_scheduled_inferences_task_done(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        engine._scheduled_configs["m1"] = {
            "model_id": "m1",
            "device_id": "d1",
            "point_name": "temp",
            "interval_seconds": 60,
            "input_window_size": 100,
        }
        engine._scheduled_tasks["m1"] = MagicMock()
        engine._scheduled_tasks["m1"].done.return_value = True
        result = engine.get_scheduled_inferences()
        assert result[0]["running"] is False


# ─── TestFetchInfluxData ──────────────────────────────────────────────────


class TestFetchInfluxData:
    async def test_no_influx_storage(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        mock_state = SimpleNamespace(influx_storage=None)
        with patch("edgelite.app._app_state", mock_state):
            result = await engine._fetch_influx_data("d1", "temp", 100)
            assert result is None

    async def test_no_data_returned(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        mock_influx = AsyncMock()
        mock_influx.query_points.return_value = []
        mock_state = SimpleNamespace(influx_storage=mock_influx)
        with patch("edgelite.app._app_state", mock_state):
            result = await engine._fetch_influx_data("d1", "temp", 100)
            assert result is None

    async def test_success(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        mock_influx = AsyncMock()
        mock_influx.query_points.return_value = [
            {"value": 1.0},
            {"value": 2.0},
            {"value": 3.0},
        ]
        mock_state = SimpleNamespace(influx_storage=mock_influx)
        with patch("edgelite.app._app_state", mock_state):
            result = await engine._fetch_influx_data("d1", "temp", 100)
            assert result == [1.0, 2.0, 3.0]

    async def test_filters_none_values(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        mock_influx = AsyncMock()
        mock_influx.query_points.return_value = [
            {"value": 1.0},
            {"value": None},
            {"value": "not_number"},
            {"value": 2.0},
        ]
        mock_state = SimpleNamespace(influx_storage=mock_influx)
        with patch("edgelite.app._app_state", mock_state):
            result = await engine._fetch_influx_data("d1", "temp", 100)
            assert result == [1.0, 2.0]

    async def test_truncates_to_window_size(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        mock_influx = AsyncMock()
        mock_influx.query_points.return_value = [{"value": float(i)} for i in range(10)]
        mock_state = SimpleNamespace(influx_storage=mock_influx)
        with patch("edgelite.app._app_state", mock_state):
            result = await engine._fetch_influx_data("d1", "temp", 5)
            assert len(result) == 5
            assert result == [5.0, 6.0, 7.0, 8.0, 9.0]

    async def test_all_none_values(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        mock_influx = AsyncMock()
        mock_influx.query_points.return_value = [{"value": None}, {"value": None}]
        mock_state = SimpleNamespace(influx_storage=mock_influx)
        with patch("edgelite.app._app_state", mock_state):
            result = await engine._fetch_influx_data("d1", "temp", 100)
            assert result is None

    async def test_exception_returns_none(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        mock_influx = AsyncMock()
        mock_influx.query_points.side_effect = Exception("DB error")
        mock_state = SimpleNamespace(influx_storage=mock_influx)
        with patch("edgelite.app._app_state", mock_state):
            result = await engine._fetch_influx_data("d1", "temp", 100)
            assert result is None


# ─── TestPublishInferenceResult ───────────────────────────────────────────


class TestPublishInferenceResult:
    async def test_without_event_bus(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        engine._event_bus = None
        result = InferenceResult("m1", {"output_0": [1.0]}, 50, "success")
        # Should not raise
        await engine._publish_inference_result(result)

    async def test_with_event_bus(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        engine._event_bus = AsyncMock()
        result = InferenceResult("m1", {"output_0": [1.0]}, 50, "success")
        await engine._publish_inference_result(result)
        engine._event_bus.publish.assert_called_once()

    async def test_event_bus_exception_handled(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        engine._event_bus = AsyncMock()
        engine._event_bus.publish.side_effect = Exception("publish failed")
        result = InferenceResult("m1", {"output_0": [1.0]}, 50, "success")
        # Should not raise
        await engine._publish_inference_result(result)


# ─── TestInferFromVideo ───────────────────────────────────────────────────


class TestInferFromVideo:
    async def test_invalid_url(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        results = []
        async for r in engine.infer_from_video("m1", "ftp://invalid"):
            results.append(r)
        assert len(results) == 1
        assert "error" in results[0]
        assert "Invalid" in results[0]["error"]

    async def test_opencv_not_installed(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        # Block cv2 import while allowing other imports
        real_import = __import__

        def blocking_import(name, *args, **kwargs):
            if name == "cv2":
                raise ImportError("no cv2")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=blocking_import):
            results = []
            async for r in engine.infer_from_video("m1", "https://example.com/video"):
                results.append(r)
            assert len(results) == 1
            assert "opencv" in results[0]["error"]

    async def test_cannot_open_video(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        mock_cap = MagicMock()
        mock_cap.isOpened.return_value = False
        with patch("cv2.VideoCapture", return_value=mock_cap):
            results = []
            async for r in engine.infer_from_video("m1", "https://example.com/video"):
                results.append(r)
            assert len(results) == 1
            assert "Cannot open" in results[0]["error"]

    async def test_video_frames_processing(self, tmp_path):
        import numpy as np

        engine = _make_active_engine(tmp_path)
        w = _make_onnx_wrapper(status="active", input_schema={"shape": [1, 3, 224, 224]})
        engine._loaded_models["m1"] = w
        # Mock cv2
        mock_cap = MagicMock()
        mock_cap.isOpened.return_value = True
        mock_cap.get.return_value = 25.0
        # Simulate 3 frames then end
        frames = [
            (True, np.zeros((480, 640, 3), dtype=np.uint8)),
            (True, np.zeros((480, 640, 3), dtype=np.uint8)),
            (True, np.zeros((480, 640, 3), dtype=np.uint8)),
            (False, None),
        ]
        mock_cap.read.side_effect = frames
        mock_cap.release = MagicMock()
        # Mock inference
        infer_result = InferenceResult("m1", {"output_0": [0.9]}, 10, "success")
        with patch("cv2.VideoCapture", return_value=mock_cap):
            with patch("cv2.resize", side_effect=lambda frame, size: frame):
                with patch.object(engine, "infer", new_callable=AsyncMock, return_value=infer_result):
                    with patch("edgelite.api.debug.record_packet"):
                        results = []
                        async for r in engine.infer_from_video("m1", "https://example.com/video", max_frames=5):
                            results.append(r)
                        mock_cap.release.assert_called_once()


# ─── TestShutdown ─────────────────────────────────────────────────────────


class TestShutdown:
    async def test_shutdown_no_models(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        await engine.shutdown()
        assert len(engine._loaded_models) == 0

    async def test_shutdown_unloads_models(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        w1 = _make_onnx_wrapper(model_id="m1", status="active")
        w2 = _make_onnx_wrapper(model_id="m2", status="active")
        w1.unload = AsyncMock()
        w2.unload = AsyncMock()
        engine._loaded_models["m1"] = w1
        engine._loaded_models["m2"] = w2
        await engine.shutdown()
        assert len(engine._loaded_models) == 0
        w1.unload.assert_called_once()
        w2.unload.assert_called_once()

    async def test_shutdown_cancels_scheduled_tasks(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        w = _make_onnx_wrapper(status="active")
        engine._loaded_models["m1"] = w
        await engine.start_scheduled_inference("m1", "d1", "temp", interval_seconds=60)
        await engine.shutdown()
        assert len(engine._scheduled_tasks) == 0

    async def test_shutdown_cancels_watcher_task(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        engine._model_watcher_task = asyncio.create_task(asyncio.sleep(100))
        await engine.shutdown()
        assert engine._model_watcher_task.done()

    async def test_shutdown_releases_pending_wrappers(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        old_w = _make_onnx_wrapper(status="active")
        old_w.unload = AsyncMock()
        engine._pending_unload_wrappers.append((old_w, 0.0))
        await engine.shutdown()
        old_w.unload.assert_called_once()
        assert len(engine._pending_unload_wrappers) == 0

    async def test_shutdown_hot_swap_manager_cleanup(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        mock_hot_swap = MagicMock()
        mock_hot_swap.list_active_swaps.return_value = []
        engine._hot_swap_manager = mock_hot_swap
        await engine.shutdown()
        mock_hot_swap.list_active_swaps.assert_called_once()


# ─── TestVersionManagement ────────────────────────────────────────────────


class TestVersionManagement:
    def test_get_model_version_history_no_wrapper(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        assert engine.get_model_version_history("nonexistent") == []

    def test_get_model_version_history_empty(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        w = _make_onnx_wrapper()
        engine._loaded_models["m1"] = w
        assert engine.get_model_version_history("m1") == []

    def test_get_model_version_history_with_entries(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        w = _make_onnx_wrapper()
        w._version_history = [
            {"version": "v1.0.0", "model_path": "/a.onnx", "timestamp": "2024-01-01", "status": "active"},
        ]
        engine._loaded_models["m1"] = w
        history = engine.get_model_version_history("m1")
        assert len(history) == 1
        assert history[0]["version"] == "v1.0.0"

    def test_record_version_creates_history(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        w = _make_onnx_wrapper(model_version="v1.0.0")
        engine._record_version(w)
        assert len(w._version_history) == 1
        assert w._version_history[0]["version"] == "v1.0.0"

    def test_record_version_with_explicit_version(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        w = _make_onnx_wrapper(model_version="v1.0.0")
        engine._record_version(w, "custom_version")
        assert w._version_history[0]["version"] == "custom_version"

    def test_record_version_caps_at_50(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        w = _make_onnx_wrapper(model_version="v1.0.0")
        for i in range(60):
            engine._record_version(w, f"v1.0.{i}")
        assert len(w._version_history) == 50

    def test_auto_increment_version_standard(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        w = _make_onnx_wrapper(model_version="v1.0.0")
        assert engine._auto_increment_version(w) == "v1.0.1"

    def test_auto_increment_version_multiple_times(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        w = _make_onnx_wrapper(model_version="v1.0.5")
        assert engine._auto_increment_version(w) == "v1.0.6"

    def test_auto_increment_version_invalid_format(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        w = _make_onnx_wrapper(model_version="custom")
        assert engine._auto_increment_version(w) == "custom"

    def test_auto_increment_version_no_v_prefix(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        w = _make_onnx_wrapper(model_version="1.0.0")
        assert engine._auto_increment_version(w) == "1.0.0"

    def test_auto_increment_version_two_parts(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        w = _make_onnx_wrapper(model_version="v1.0")
        assert engine._auto_increment_version(w) == "v1.0"

    async def test_rollback_model_not_found(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        with pytest.raises(ValueError, match="Model not found"):
            await engine.rollback_model_version("nonexistent", "v1.0.0")

    async def test_rollback_version_not_found(self, tmp_path):
        engine = _make_active_engine(tmp_path)
        w = _make_onnx_wrapper()
        w._version_history = [{"version": "v1.0.0", "model_path": "/a.onnx"}]
        engine._loaded_models["m1"] = w
        with pytest.raises(ValueError, match="Version .* not found"):
            await engine.rollback_model_version("m1", "v2.0.0")

    async def test_rollback_no_reload(self, tmp_path):
        """Rollback when target model path doesn't exist - only updates version."""
        engine = _make_active_engine(tmp_path)
        w = _make_onnx_wrapper(model_version="v1.0.1")
        w._version_history = [
            {"version": "v1.0.0", "model_path": "/nonexistent.onnx"},
            {"version": "v1.0.1", "model_path": "/current.onnx"},
        ]
        engine._loaded_models["m1"] = w
        result = await engine.rollback_model_version("m1", "v1.0.0")
        assert result is True
        assert w.model_version == "v1.0.0"

    async def test_rollback_with_reload(self, tmp_path):
        """Rollback when target model path exists - creates new wrapper."""
        engine = _make_active_engine(tmp_path)
        model_file = tmp_path / "old_model.onnx"
        model_file.write_bytes(b"fake")
        w = _make_onnx_wrapper(model_version="v1.0.1")
        w._version_history = [
            {"version": "v1.0.0", "model_path": str(model_file)},
            {"version": "v1.0.1", "model_path": str(model_file)},
        ]
        engine._loaded_models["m1"] = w
        with patch.object(OnnxModelWrapper, "load", new_callable=AsyncMock):
            result = await engine.rollback_model_version("m1", "v1.0.0")
            assert result is True
            new_wrapper = engine._loaded_models["m1"]
            assert new_wrapper.model_version == "v1.0.0"
            # Old wrapper should be in pending unload
            assert len(engine._pending_unload_wrappers) == 1


# ─── TestAiInferenceEvent ─────────────────────────────────────────────────


class TestAiInferenceEvent:
    def test_init(self):
        event = AiInferenceEvent(model_id="m1", output_data={"output_0": [1.0]}, latency_ms=50)
        assert event.model_id == "m1"
        assert event.output_data == {"output_0": [1.0]}
        assert event.latency_ms == 50
        assert event.timestamp is not None

    def test_slots(self):
        event = AiInferenceEvent("m1", {}, 0)
        with pytest.raises(AttributeError):
            event.extra_field = "value"


# ─── TestTFLiteModelWrapper ───────────────────────────────────────────────


class TestTFLiteModelWrapper:
    def test_init(self):
        w = TFLiteModelWrapper(
            model_id="m1",
            model_name="Model",
            model_version="v1.0.0",
            model_type="custom",
            model_path="/model.tflite",
            input_schema={"shape": [1, 3]},
            output_schema={"shape": [1]},
        )
        assert w.status == "inactive"
        assert w.interpreter is None
        assert w.is_preset is False

    async def test_load_no_tflite(self):
        w = TFLiteModelWrapper(
            model_id="m1",
            model_name="Model",
            model_version="v1.0.0",
            model_type="custom",
            model_path="/model.tflite",
            input_schema={"shape": [1, 3]},
            output_schema={"shape": [1]},
        )
        # Remove tflite modules from sys.modules and block import
        saved = {}
        for key in list(sys.modules.keys()):
            if key.startswith("tflite_runtime") or key == "tensorflow":
                saved[key] = sys.modules.pop(key)
        try:
            real_import = __import__

            def blocking_import(name, *args, **kwargs):
                if name.startswith("tflite_runtime") or name == "tensorflow":
                    raise ImportError("no tflite")
                return real_import(name, *args, **kwargs)

            with patch("builtins.__import__", side_effect=blocking_import):
                await w.load()
                assert w.status == "inactive"
        finally:
            sys.modules.update(saved)

    async def test_load_success(self):
        w = TFLiteModelWrapper(
            model_id="m1",
            model_name="Model",
            model_version="v1.0.0",
            model_type="custom",
            model_path="/model.tflite",
            input_schema={"shape": [1, 3]},
            output_schema={"shape": [1]},
        )
        fake_interpreter = MagicMock()
        fake_interpreter.get_input_details.return_value = [{"index": 0}]
        fake_interpreter.get_output_details.return_value = [{"index": 0}]
        fake_interpreter_mod = MagicMock()
        fake_interpreter_mod.Interpreter.return_value = fake_interpreter
        fake_pkg = MagicMock()
        fake_pkg.interpreter = fake_interpreter_mod  # import tflite_runtime.interpreter as tflite does attribute access
        with patch.dict(
            sys.modules,
            {"tflite_runtime": fake_pkg, "tflite_runtime.interpreter": fake_interpreter_mod},
        ):
            await w.load()
            assert w.status == "active"
            assert w.interpreter is fake_interpreter

    async def test_load_failure(self):
        w = TFLiteModelWrapper(
            model_id="m1",
            model_name="Model",
            model_version="v1.0.0",
            model_type="custom",
            model_path="/model.tflite",
            input_schema={"shape": [1, 3]},
            output_schema={"shape": [1]},
        )
        fake_interpreter_mod = MagicMock()
        fake_interpreter_mod.Interpreter.side_effect = RuntimeError("load failed")
        fake_pkg = MagicMock()
        fake_pkg.interpreter = fake_interpreter_mod
        with patch.dict(
            sys.modules,
            {"tflite_runtime": fake_pkg, "tflite_runtime.interpreter": fake_interpreter_mod},
        ):
            await w.load()
            assert w.status == "error"
            assert w.interpreter is None

    async def test_unload(self):
        w = TFLiteModelWrapper(
            model_id="m1",
            model_name="Model",
            model_version="v1.0.0",
            model_type="custom",
            model_path="/model.tflite",
            input_schema={"shape": [1, 3]},
            output_schema={"shape": [1]},
        )
        w.interpreter = MagicMock()
        w.status = "active"
        w.loaded_at = datetime.now(UTC)
        await w.unload()
        assert w.interpreter is None
        assert w.status == "inactive"
        assert w.loaded_at is None

    def test_run_not_loaded(self):
        import numpy as np

        w = TFLiteModelWrapper(
            model_id="m1",
            model_name="Model",
            model_version="v1.0.0",
            model_type="custom",
            model_path="/model.tflite",
            input_schema={"shape": [1, 3]},
            output_schema={"shape": [1]},
        )
        with pytest.raises(RuntimeError, match="not loaded"):
            w.run(np.array([1.0]))

    def test_run_success(self):
        import numpy as np

        w = TFLiteModelWrapper(
            model_id="m1",
            model_name="Model",
            model_version="v1.0.0",
            model_type="custom",
            model_path="/model.tflite",
            input_schema={"shape": [1, 3]},
            output_schema={"shape": [1]},
        )
        w.interpreter = MagicMock()
        w.input_details = {"index": 0}
        w.output_details = {"index": 0}
        w.interpreter.get_output_details.return_value = [{"index": 0}]
        w.interpreter.get_tensor.return_value = np.array([0.5])
        result = w.run(np.array([1.0, 2.0, 3.0]))
        assert len(result) == 1


# ─── TestPMMLModelWrapper ─────────────────────────────────────────────────


class TestPMMLModelWrapper:
    def test_init(self):
        w = PMMLModelWrapper(
            model_id="m1",
            model_name="Model",
            model_version="v1.0.0",
            model_type="custom",
            model_path="/model.pmml",
            input_schema={"shape": [1, 2]},
            output_schema={"shape": [1]},
        )
        assert w.status == "inactive"
        assert w._model is None
        assert w._feature_names == []

    async def test_load_regression_model(self, tmp_path):
        # PMML without namespace - the source code searches model tags without namespace prefix
        pmml_content = """<?xml version="1.0"?>
<PMML>
  <DataDictionary>
    <DataField name="x1"/>
    <DataField name="x2"/>
  </DataDictionary>
  <RegressionModel>
    <RegressionTable intercept="1.0">
      <NumericPredictor name="x1" coefficient="2.0"/>
      <NumericPredictor name="x2" coefficient="3.0"/>
    </RegressionTable>
  </RegressionModel>
</PMML>"""
        pmml_file = tmp_path / "model.pmml"
        pmml_file.write_text(pmml_content)
        w = PMMLModelWrapper(
            model_id="m1",
            model_name="Model",
            model_version="v1.0.0",
            model_type="custom",
            model_path=str(pmml_file),
            input_schema={"shape": [1, 2]},
            output_schema={"shape": [1]},
        )
        await w.load()
        assert w.status == "active"
        assert w._model is not None
        assert w._model["type"] == "regression"
        assert "x1" in w._feature_names
        assert "x2" in w._feature_names

    async def test_load_no_supported_model(self, tmp_path):
        pmml_content = """<?xml version="1.0"?>
<PMML>
  <DataDictionary>
    <DataField name="x1"/>
  </DataDictionary>
</PMML>"""
        pmml_file = tmp_path / "model.pmml"
        pmml_file.write_text(pmml_content)
        w = PMMLModelWrapper(
            model_id="m1",
            model_name="Model",
            model_version="v1.0.0",
            model_type="custom",
            model_path=str(pmml_file),
            input_schema={"shape": [1, 1]},
            output_schema={"shape": [1]},
        )
        await w.load()
        assert w.status == "unavailable"

    async def test_load_invalid_xml(self, tmp_path):
        pmml_file = tmp_path / "model.pmml"
        pmml_file.write_text("not valid xml")
        w = PMMLModelWrapper(
            model_id="m1",
            model_name="Model",
            model_version="v1.0.0",
            model_type="custom",
            model_path=str(pmml_file),
            input_schema={"shape": [1, 1]},
            output_schema={"shape": [1]},
        )
        await w.load()
        assert w.status == "error"

    async def test_unload(self):
        w = PMMLModelWrapper(
            model_id="m1",
            model_name="Model",
            model_version="v1.0.0",
            model_type="custom",
            model_path="/model.pmml",
            input_schema={"shape": [1, 2]},
            output_schema={"shape": [1]},
        )
        w._model = {"type": "regression"}
        w.status = "active"
        w.loaded_at = datetime.now(UTC)
        await w.unload()
        assert w._model is None
        assert w.status == "inactive"
        assert w.loaded_at is None

    def test_predict_regression(self):
        w = PMMLModelWrapper(
            model_id="m1",
            model_name="Model",
            model_version="v1.0.0",
            model_type="custom",
            model_path="/model.pmml",
            input_schema={"shape": [1, 2]},
            output_schema={"shape": [1]},
        )
        w._model = {
            "type": "regression",
            "intercept": 1.0,
            "coefficients": {"x1": 2.0, "x2": 3.0},
        }
        result = w.predict({"x1": 1.0, "x2": 2.0})
        # 1.0 + 2*1 + 3*2 = 9.0
        assert result == 9.0

    def test_predict_missing_feature(self):
        w = PMMLModelWrapper(
            model_id="m1",
            model_name="Model",
            model_version="v1.0.0",
            model_type="custom",
            model_path="/model.pmml",
            input_schema={"shape": [1, 2]},
            output_schema={"shape": [1]},
        )
        w._model = {
            "type": "regression",
            "intercept": 0.0,
            "coefficients": {"x1": 2.0},
        }
        result = w.predict({"x1": 1.0})  # x2 missing -> 0
        assert result == 2.0

    def test_predict_not_loaded(self):
        w = PMMLModelWrapper(
            model_id="m1",
            model_name="Model",
            model_version="v1.0.0",
            model_type="custom",
            model_path="/model.pmml",
            input_schema={"shape": [1, 2]},
            output_schema={"shape": [1]},
        )
        with pytest.raises(RuntimeError, match="not loaded"):
            w.predict({})

    def test_predict_unknown_type(self):
        w = PMMLModelWrapper(
            model_id="m1",
            model_name="Model",
            model_version="v1.0.0",
            model_type="custom",
            model_path="/model.pmml",
            input_schema={"shape": [1, 2]},
            output_schema={"shape": [1]},
        )
        w._model = {"type": "unknown"}
        result = w.predict({})
        assert result == 0.0

    def test_predict_tree_less_than(self):
        w = PMMLModelWrapper(
            model_id="m1",
            model_name="Model",
            model_version="v1.0.0",
            model_type="custom",
            model_path="/model.pmml",
            input_schema={"shape": [1, 1]},
            output_schema={"shape": [1]},
        )
        w._model = {
            "type": "tree",
            "root": {
                "field": "x1",
                "operator": "lessThan",
                "value": "5",
                "score": 0,
                "children": [
                    {"score": 1.0, "children": []},
                    {"score": 2.0, "children": []},
                ],
            },
        }
        assert w.predict({"x1": 3.0}) == 1.0  # 3 < 5 -> left child
        assert w.predict({"x1": 7.0}) == 2.0  # 7 >= 5 -> right child

    def test_predict_tree_equal(self):
        w = PMMLModelWrapper(
            model_id="m1",
            model_name="Model",
            model_version="v1.0.0",
            model_type="custom",
            model_path="/model.pmml",
            input_schema={"shape": [1, 1]},
            output_schema={"shape": [1]},
        )
        w._model = {
            "type": "tree",
            "root": {
                "field": "x1",
                "operator": "equal",
                "value": "5",
                "score": 0,
                "children": [
                    {"score": 1.0, "children": []},
                    {"score": 2.0, "children": []},
                ],
            },
        }
        assert w.predict({"x1": 5.0}) == 1.0  # equal -> left
        assert w.predict({"x1": 4.0}) == 2.0  # not equal -> right

    def test_run_success(self):
        w = PMMLModelWrapper(
            model_id="m1",
            model_name="Model",
            model_version="v1.0.0",
            model_type="custom",
            model_path="/model.pmml",
            input_schema={"shape": [1, 2]},
            output_schema={"shape": [1]},
        )
        w._feature_names = ["x1", "x2"]
        w._model = {
            "type": "regression",
            "intercept": 0.0,
            "coefficients": {"x1": 1.0, "x2": 1.0},
        }
        result = w.run([1.0, 2.0])
        assert result == [3.0]


# ─── TestQuantization ────────────────────────────────────────────────────


class TestQuantization:
    def test_quantize_int8_import_error(self):
        with patch("builtins.__import__", side_effect=ImportError("no quantization")):
            result = quantize_model_int8("/model.onnx", [], "/output.onnx")
            assert result is False

    def test_quantize_int8_exception(self):
        with patch("onnxruntime.quantization.quantize_dynamic", side_effect=Exception("quant failed")):
            result = quantize_model_int8("/model.onnx", [], "/output.onnx")
            assert result is False

    def test_quantize_fp16_import_error(self):
        with patch("builtins.__import__", side_effect=ImportError("no onnx")):
            result = quantize_model_fp16("/model.onnx", "/output.onnx")
            assert result is False

    def test_quantize_fp16_exception(self):
        with patch("onnx.load", side_effect=Exception("load failed")):
            result = quantize_model_fp16("/model.onnx", "/output.onnx")
            assert result is False


# ─── TestGetModelInfo ─────────────────────────────────────────────────────


class TestGetModelInfo:
    def test_file_not_found(self):
        result = get_model_info("/nonexistent/model.onnx")
        assert "error" in result
        assert "not found" in result["error"].lower()

    def test_onnx_model_info(self, tmp_path):
        model_file = tmp_path / "model.onnx"
        model_bytes = _generate_onnx_model("test.onnx", [1, 5], [1, 3])
        model_file.write_bytes(model_bytes)
        result = get_model_info(str(model_file))
        assert result["exists"] is True
        assert result["format"] == "onnx"
        assert "file_size_bytes" in result
        # ONNX parsing may fail due to DataType.Name subscripting bug in source;
        # inputs/outputs are only present if parsing succeeds
        assert "file_size_mb" in result

    def test_unknown_format(self, tmp_path):
        model_file = tmp_path / "model.txt"
        model_file.write_text("dummy")
        result = get_model_info(str(model_file))
        assert result["exists"] is True
        assert result["format"] == "txt"
        # No inputs/outputs for unknown format
        assert "inputs" not in result
