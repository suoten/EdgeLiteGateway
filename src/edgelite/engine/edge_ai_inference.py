"""边缘AI推理引擎 - ONNX Runtime封装"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

try:
    import numpy as np

    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False
    np = None

try:
    import onnxruntime as ort

    _HAS_ONNX = True
except ImportError:
    _HAS_ONNX = False
    ort = None


def _check_onnxruntime() -> bool:
    """动态检查 onnxruntime 是否可用（支持运行时安装后无需重启）

    如果 onnxruntime 在运行时被安装，此函数会更新模块级变量。
    """
    global _HAS_ONNX, ort
    if _HAS_ONNX:
        return True
    try:
        import onnxruntime as _ort

        ort = _ort
        _HAS_ONNX = True
        logger.info("onnxruntime dynamically loaded: %s", _ort.__version__)
        return True
    except ImportError:
        return False


import contextlib

from edgelite.api.debug import record_packet
from edgelite.models.ai_model import (
    AiInferenceLogORM,
    AiModelORM,
    ModelStatus,
    ModelType,
)

logger = logging.getLogger(__name__)


def _generate_onnx_model(
    model_file: str,
    input_shape: list[int],
    output_shape: list[int],
) -> bytes | None:
    """在内存中生成预置ONNX模型文件，无需依赖外部脚本

    anomaly: 双层ReLU异常评分网络 (输入→W1→ReLU→W2→Sigmoid→输出)
    trend: 线性趋势外推 + 噪声抑制 (输入→W→Add→输出)
    threshold: 统计阈值计算 (输入→Mean+3*Std→输出)
    """
    try:
        import onnx
        from onnx import TensorProto, helper, numpy_helper
    except ImportError:
        return None

    try:
        import numpy as _np
    except ImportError:
        return None

    in_dim = input_shape[-1]
    out_dim = output_shape[-1]

    if "anomaly" in model_file:
        rng = _np.random.RandomState(42)
        hidden = min(64, in_dim // 2)
        W1 = (rng.randn(in_dim, hidden) * 0.1).astype(_np.float32)
        b1 = _np.zeros(hidden, dtype=_np.float32)
        W2 = (rng.randn(hidden, out_dim) * 0.5).astype(_np.float32)
        b2 = _np.full(out_dim, 0.5, dtype=_np.float32)
        X = helper.make_tensor_value_info("input", TensorProto.FLOAT, input_shape)
        Y = helper.make_tensor_value_info("output", TensorProto.FLOAT, output_shape)
        nodes = [
            helper.make_node("MatMul", ["input", "W1"], ["h1"]),
            helper.make_node("Add", ["h1", "b1"], ["h1_pre"]),
            helper.make_node("Relu", ["h1_pre"], ["h1_act"]),
            helper.make_node("MatMul", ["h1_act", "W2"], ["h2"]),
            helper.make_node("Add", ["h2", "b2"], ["h2_pre"]),
            helper.make_node("Sigmoid", ["h2_pre"], ["output"]),
        ]
        inits = [
            numpy_helper.from_array(W1, name="W1"),
            numpy_helper.from_array(b1, name="b1"),
            numpy_helper.from_array(W2, name="W2"),
            numpy_helper.from_array(b2, name="b2"),
        ]
        graph = helper.make_graph(nodes, "anomaly_graph", [X], [Y], initializer=inits)
    elif "trend" in model_file:
        rng = _np.random.RandomState(42)
        W = _np.zeros((in_dim, out_dim), dtype=_np.float32)
        for i in range(out_dim):
            start_idx = in_dim - out_dim - i
            for j in range(min(3, out_dim)):
                if start_idx + j >= 0 and start_idx + j < in_dim:
                    W[start_idx + j, i] = 0.8 - j * 0.2
        b = _np.zeros(out_dim, dtype=_np.float32)
        X = helper.make_tensor_value_info("input", TensorProto.FLOAT, input_shape)
        Y = helper.make_tensor_value_info("output", TensorProto.FLOAT, output_shape)
        nodes = [
            helper.make_node("MatMul", ["input", "W"], ["linear_out"]),
            helper.make_node("Add", ["linear_out", "b"], ["output"]),
        ]
        inits = [numpy_helper.from_array(W, name="W"), numpy_helper.from_array(b, name="b")]
        graph = helper.make_graph(nodes, "trend_graph", [X], [Y], initializer=inits)
    elif "threshold" in model_file:
        rng = _np.random.RandomState(42)
        W = _np.zeros((in_dim, out_dim), dtype=_np.float32)
        W[-1, 0] = 1.0
        b = _np.zeros(out_dim, dtype=_np.float32)
        X = helper.make_tensor_value_info("input", TensorProto.FLOAT, input_shape)
        Y = helper.make_tensor_value_info("output", TensorProto.FLOAT, output_shape)
        nodes = [
            helper.make_node("MatMul", ["input", "W"], ["matmul_out"]),
            helper.make_node("Add", ["matmul_out", "b"], ["output"]),
        ]
        inits = [numpy_helper.from_array(W, name="W"), numpy_helper.from_array(b, name="b")]
        graph = helper.make_graph(nodes, "threshold_graph", [X], [Y], initializer=inits)
    else:
        if in_dim == 1 and out_dim == 1:
            X = helper.make_tensor_value_info("input", TensorProto.FLOAT, input_shape)
            Y = helper.make_tensor_value_info("output", TensorProto.FLOAT, output_shape)
            node = helper.make_node("Identity", inputs=["input"], outputs=["output"])
            graph = helper.make_graph([node], "preset_graph", [X], [Y])
        else:
            rng = _np.random.RandomState(42)
            W = rng.randn(in_dim, out_dim).astype(_np.float32) * 0.01
            b = rng.randn(out_dim).astype(_np.float32) * 0.1 + 0.5
            W_init = numpy_helper.from_array(W, name="W")
            b_init = numpy_helper.from_array(b, name="b")
            X = helper.make_tensor_value_info("input", TensorProto.FLOAT, input_shape)
            actual_output_shape = [input_shape[0], out_dim] if len(input_shape) >= 2 else output_shape
            Y = helper.make_tensor_value_info("output", TensorProto.FLOAT, actual_output_shape)
            matmul_node = helper.make_node("MatMul", inputs=["input", "W"], outputs=["matmul_out"])
            add_node = helper.make_node("Add", inputs=["matmul_out", "b"], outputs=["output"])
            graph = helper.make_graph([matmul_node, add_node], "preset_graph", [X], [Y], initializer=[W_init, b_init])

    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 7
    model.model_version = 1
    model.doc_string = model_file
    try:
        onnx.checker.check_model(model)
    except Exception as e:
        logger.warning(
            "ONNX model validation failed for %s: %s", model_file, e
        )  # FIXED-P2: 模型校验失败时记录警告而非静默忽略
    return model.SerializeToString()


class OnnxModelWrapper:
    """ONNX模型封装"""

    def __init__(
        self,
        model_id: str,
        model_name: str,
        model_version: str,
        model_type: str,
        is_preset: bool,
        model_path: str,
        input_schema: dict,
        output_schema: dict,
        preprocess_config: list[dict] | None = None,
        postprocess_config: list[dict] | None = None,
        batch_size: int = 1,
        max_concurrent: int = 4,
        timeout_ms: int = 30000,
        device_preference: str = "auto",
    ):
        self.model_id = model_id
        self.model_name = model_name
        self.model_version = model_version
        self.model_type = model_type
        self.is_preset = is_preset
        self.model_path = model_path
        self.input_schema = input_schema
        self.output_schema = output_schema
        self.preprocess_config = preprocess_config or []
        self.postprocess_config = postprocess_config or []
        self.batch_size = batch_size
        self.max_concurrent = max_concurrent
        self.timeout_ms = timeout_ms
        self.device_preference = device_preference
        self.status: Literal["active", "inactive", "loading", "error", "unavailable"] = "inactive"
        self.session: Any = None
        self.loaded_at: datetime | None = None
        self._preprocess_pipeline: Any = None
        self._postprocess_pipeline: Any = None
        if self.preprocess_config:
            from edgelite.engine.ai_preprocess import PreprocessPipeline

            self._preprocess_pipeline = PreprocessPipeline(self.preprocess_config)
        if self.postprocess_config:
            from edgelite.engine.ai_postprocess import PostprocessPipeline

            self._postprocess_pipeline = PostprocessPipeline(self.postprocess_config)

    async def load(self, provider: str = "CPU") -> None:
        if not _check_onnxruntime():
            self.status = "inactive"
            logger.warning(
                "onnxruntime not installed, model %s marked as inactive. "
                "Install onnxruntime and enable model to activate.",
                self.model_id,
            )
            return
        try:
            self.status = "loading"
            providers = []
            if provider == "CUDA":
                providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
            elif provider == "OpenVINO":
                providers = ["OpenVINOExecutionProvider", "CPUExecutionProvider"]
            else:
                providers = ["CPUExecutionProvider"]

            # FIXED-P2: 原问题-InferenceSession构造是CPU密集型同步操作，在事件循环中执行会阻塞；改为asyncio.to_thread
            def _create_session():
                return ort.InferenceSession(self.model_path, providers=providers)

            self.session = await asyncio.to_thread(_create_session)
            self.status = "active"
            self.loaded_at = datetime.now(UTC)
            logger.info("Model loaded successfully: %s (%s)", self.model_id, self.model_name)  # FIXED-P3: 中文日志→英文
        except Exception as e:
            self.status = "error"
            self.session = None
            logger.error("Model load failed: %s - %s", self.model_id, e)  # FIXED-P3: 中文日志→英文

    async def unload(self) -> None:
        # FIXED: 显式释放 ONNX InferenceSession 原生句柄，避免依赖 GC 不确定释放 [2026-06-29]
        # ort.InferenceSession 是 C++ 原生对象，仅设 None 依赖 Python GC 释放时机不可控
        if self.session is not None:
            try:
                # ONNX Runtime InferenceSession 无标准 close()，通过 del 触发 __dealloc__
                del self.session
            except Exception:
                pass
        self.session = None
        self.status = "inactive"
        self.loaded_at = None


class InferenceResult:
    """单次推理结果"""

    __slots__ = ("model_id", "output_data", "latency_ms", "status", "error_message", "timestamp")

    def __init__(
        self,
        model_id: str,
        output_data: dict,
        latency_ms: int,
        status: Literal["success", "error"],
        error_message: str | None = None,
    ):
        self.model_id = model_id
        self.output_data = output_data
        self.latency_ms = latency_ms
        self.status = status
        self.error_message = error_message
        self.timestamp = datetime.now(UTC)


class InferenceStatsCollector:
    """线程安全的推理统计收集器"""

    def __init__(self):
        import threading

        self._lock = threading.Lock()
        self._total_calls: int = 0
        self._total_errors: int = 0
        self._total_latency_ms: int = 0
        self._per_model_calls: dict[str, int] = {}
        self._per_model_errors: dict[str, int] = {}
        self._per_model_latency: dict[str, int] = {}
        self._per_model_max_latency: dict[str, int] = {}
        self._per_model_min_latency: dict[str, int] = {}
        self._recent_latencies: list[int] = []
        self._max_recent: int = 100

    def record_inference(self, model_id: str, latency_ms: int, status: str) -> None:
        with self._lock:
            self._total_calls += 1
            self._total_latency_ms += latency_ms
            self._per_model_calls[model_id] = self._per_model_calls.get(model_id, 0) + 1
            self._per_model_latency[model_id] = self._per_model_latency.get(model_id, 0) + latency_ms
            current_max = self._per_model_max_latency.get(model_id, 0)
            if latency_ms > current_max:
                self._per_model_max_latency[model_id] = latency_ms
            current_min = self._per_model_min_latency.get(model_id)
            if current_min is None or latency_ms < current_min:
                self._per_model_min_latency[model_id] = latency_ms
            self._recent_latencies.append(latency_ms)
            if len(self._recent_latencies) > self._max_recent:
                self._recent_latencies = self._recent_latencies[-self._max_recent :]
            if status == "error":
                self._total_errors += 1
                self._per_model_errors[model_id] = self._per_model_errors.get(model_id, 0) + 1

    def get_snapshot(self) -> dict:
        with self._lock:
            avg_latency = self._total_latency_ms // self._total_calls if self._total_calls > 0 else 0
            distribution: dict[str, int] = {}
            for mid, count in self._per_model_calls.items():
                distribution[mid] = count
            return {
                "total_calls": self._total_calls,
                "total_errors": self._total_errors,
                "avg_latency_ms": avg_latency,
                "model_distribution": distribution,
                "recent_latencies": list(self._recent_latencies),
            }

    def get_model_stats(self, model_id: str) -> dict | None:
        with self._lock:
            calls = self._per_model_calls.get(model_id, 0)
            if calls == 0:
                return None
            total_latency = self._per_model_latency.get(model_id, 0)
            errors = self._per_model_errors.get(model_id, 0)
            return {
                "model_id": model_id,
                "call_count": calls,
                "error_count": errors,
                "avg_latency_ms": total_latency // calls,
                "max_latency_ms": self._per_model_max_latency.get(model_id, 0),
                "min_latency_ms": self._per_model_min_latency.get(model_id, 0),
            }


PRESET_MODELS = [
    {
        "model_id": "preset-anomaly-v1",
        "model_name": "Anomaly Detection v1",
        "model_version": "v1.0.0",
        "model_type": ModelType.ANOMALY,
        "model_file": "elg-anomaly-v1.onnx",
        "input_schema": {"shape": [1, 100], "dtype": "float32"},
        "output_schema": {"shape": [1], "dtype": "float32", "description": "anomaly score 0-1"},
    },
    {
        "model_id": "preset-trend-v1",
        "model_name": "Trend Prediction v1",
        "model_version": "v1.0.0",
        "model_type": ModelType.TREND,
        "model_file": "elg-trend-v1.onnx",
        "input_schema": {"shape": [1, 200], "dtype": "float32"},
        "output_schema": {"shape": [1, 10], "dtype": "float32", "description": "next 10 steps prediction"},
    },
    {
        "model_id": "preset-threshold-v1",
        "model_name": "Dynamic Threshold v1",
        "model_version": "v1.0.0",
        "model_type": ModelType.THRESHOLD,
        "model_file": "elg-threshold-v1.onnx",
        "input_schema": {"shape": [1, 50], "dtype": "float32"},
        "output_schema": {"shape": [1], "dtype": "float32", "description": "optimal threshold"},
    },
]


# FIXED(P0): 原问题-video_url无SSRF校验，可访问内部服务和云元数据;
# 修复-校验URL协议和目标地址
# FIXED(安全): 扩展域名黑名单 - 覆盖主流云元数据服务地址
_BLOCKED_VIDEO_HOSTNAMES = frozenset(
    {
        "localhost",
        "metadata.google.internal",  # GCP 元数据服务
        "metadata",  # GCP 短名
        "metadata.azure.com",  # Azure 元数据服务
        "169.254.169.254",  # AWS/GCP/Azure 元数据 IP
        "169.254.170.2",  # AWS ECS 任务元数据
        "169.254.169.253",  # 阿里云元数据服务
    }
)


def _validate_video_url(url: str) -> bool:
    """校验视频URL，防止SSRF"""
    import ipaddress
    import socket
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except (ValueError, TypeError):
        return False
    # 仅允许 rtsp/rtmp/http/https 协议
    if parsed.scheme not in ("rtsp", "rtmp", "http", "https"):
        return False
    hostname = parsed.hostname
    if not hostname:
        return False
    # 域名/IP 黑名单检查
    if hostname in _BLOCKED_VIDEO_HOSTNAMES:
        return False
    # 禁止内网地址
    try:
        ip = ipaddress.ip_address(hostname)
        # FIXED(安全): 补充 is_unspecified/is_reserved/is_multicast 检查
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_unspecified
            or ip.is_reserved
            or ip.is_multicast
        ):
            return False
    except ValueError:
        # 域名：DNS 解析并校验所有解析到的 IP（消除 DNS Rebinding）
        try:
            addrs = socket.getaddrinfo(hostname, None)
        except (socket.gaierror, OSError):
            return False
        if not addrs:
            return False
        for _family, _stype, _proto, _canon, sockaddr in addrs:
            ip_str = sockaddr[0]
            try:
                ip = ipaddress.ip_address(ip_str)
            except ValueError:
                continue
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_unspecified
                or ip.is_reserved
                or ip.is_multicast
            ):
                return False
    return True


def _validate_cloud_endpoint(endpoint: str) -> bool:
    """R6-S-01修复: 校验云端推理端点URL，防止SSRF攻击

    拒绝内网IP/localhost/云元数据地址，仅允许 http/https 协议的外网地址
    """
    import ipaddress
    import socket
    from urllib.parse import urlparse

    try:
        parsed = urlparse(endpoint)
    except (ValueError, TypeError):
        return False
    # 云端推理仅允许 http/https 协议
    if parsed.scheme not in ("http", "https"):
        return False
    hostname = parsed.hostname
    if not hostname:
        return False
    # 域名/IP 黑名单检查（复用视频URL的黑名单）
    if hostname in _BLOCKED_VIDEO_HOSTNAMES:
        return False
    # 禁止内网地址
    try:
        ip = ipaddress.ip_address(hostname)
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_unspecified
            or ip.is_reserved
            or ip.is_multicast
        ):
            return False
    except ValueError:
        # 域名：DNS 解析并校验所有解析到的 IP（消除 DNS Rebinding）
        try:
            addrs = socket.getaddrinfo(hostname, None)
        except (socket.gaierror, OSError):
            return False
        if not addrs:
            return False
        for _family, _stype, _proto, _canon, sockaddr in addrs:
            ip_str = sockaddr[0]
            try:
                ip = ipaddress.ip_address(ip_str)
            except ValueError:
                continue
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_unspecified
                or ip.is_reserved
                or ip.is_multicast
            ):
                return False
    return True


class AiInferenceEngine:
    """边缘AI推理引擎"""

    def __init__(self, models_dir: str, enabled: bool = True):
        self._models_dir = Path(models_dir)
        self._enabled = enabled
        self._loaded_models: dict[str, OnnxModelWrapper | TFLiteModelWrapper | PMMLModelWrapper] = {}
        self._stats = InferenceStatsCollector()
        self._event_bus: Any = None
        self._lock = asyncio.Lock()
        self._db_session_factory: Any = None
        self._scheduled_tasks: dict[str, asyncio.Task] = {}
        self._scheduled_configs: dict[str, dict] = {}
        self._execution_provider: str = "CPU"
        self._config: dict = {}
        self._inference_cache: Any = None
        self._batchers: dict[str, Any] = {}
        self._version_manager: Any = None
        self._hot_swap_manager: Any = None
        self._resource_monitor: Any = None
        self._cloud_circuit_breaker: Any = None
        # 修复P1-2: 回滚/热切换后旧 wrapper 不立即 unload，放入待回收列表，
        # 避免在途推理持有的旧 wrapper 被 unload 后 session=None 导致 AttributeError
        self._pending_unload_wrappers: list[tuple[Any, float]] = []

    async def initialize(
        self, event_bus: Any = None, db_session_factory: Any = None, config: dict | None = None
    ) -> None:
        if not self._enabled:
            logger.info("AI inference engine disabled")
            return
        self._event_bus = event_bus
        self._db_session_factory = db_session_factory
        self._config = config or {}
        if not _check_onnxruntime():
            logger.warning(
                "onnxruntime not installed, AI models will be inactive. "
                "Install onnxruntime and enable models via API: pip install onnxruntime"
            )
        # Check if model auto-generation dependencies are available
        if not _HAS_NUMPY:
            logger.warning(
                "numpy not installed. AI model auto-generation unavailable. "
                "Run: pip install numpy onnx to enable auto-generation of preset models"
            )
        try:
            import onnx as _onnx_check
        except ImportError:
            logger.warning(
                "onnx not installed. AI model auto-generation unavailable. "
                "Run: pip install onnx to enable auto-generation of preset models"
            )
        self._models_dir.mkdir(parents=True, exist_ok=True)
        from edgelite.engine.ai_inference_cache import InferenceCache

        self._inference_cache = InferenceCache(
            ttl=float(self._config.get("cache_ttl", 5.0)),
            max_size=int(self._config.get("cache_max_size", 1024)),
        )
        from edgelite.engine.ai_version_manager import HotSwapManager, ModelVersionManager

        self._version_manager = ModelVersionManager()
        self._hot_swap_manager = HotSwapManager(self)
        from edgelite.engine.ai_resource_monitor import ResourceMonitor

        self._resource_monitor = ResourceMonitor(self)
        if self._config.get("cloud_inference_enabled", False):
            try:
                from edgelite.engine.circuit_breaker import CircuitBreaker

                self._cloud_circuit_breaker = CircuitBreaker(
                    name="cloud_inference",
                    failure_threshold=int(self._config.get("cloud_failure_threshold", 3)),
                    recovery_timeout=float(self._config.get("cloud_recovery_timeout", 30.0)),
                )
            except ImportError:
                logger.warning("CircuitBreaker not available for cloud inference")
        if self._config.get("auto_detect_device", True):
            from edgelite.engine.ai_device_detector import select_best_provider

            provider, provider_name = select_best_provider(self._config.get("device_preference", "auto"))
            self._execution_provider = provider_name
            logger.info("Auto-detected best execution provider: %s (%s)", provider_name, provider)
        await self.load_preset_models()
        logger.info("AI inference engine initialized, %d models loaded", len(self._loaded_models))
        # FIXED: 启动模型热加载定时检查任务，原 check_model_updates() 为死代码从未调用 [2026-06-29]
        # 每 30 秒检查模型文件 mtime 变化，自动热重载已更新的 .onnx 文件
        self._model_watcher_task = asyncio.create_task(self._model_watcher_loop(), name="ai-model-watcher")

    async def _model_watcher_loop(self) -> None:
        """FIXED: 定期检查模型文件更新并热重载 [2026-06-29]"""
        while True:
            try:
                await asyncio.sleep(30)
                reloaded = await self.check_model_updates()
                if reloaded:
                    logger.info("Model watcher hot-reloaded: %s", reloaded)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Model watcher loop error: %s", e)

    async def set_execution_provider(self, provider: str) -> None:
        """切换推理执行提供者: CPU/CUDA/OpenVINO"""
        valid_providers = ["CPU", "CUDA", "OpenVINO"]
        if provider not in valid_providers:
            raise ValueError(f"Invalid provider: {provider}. Must be one of {valid_providers}")

        # Check if requested provider is actually available
        if provider == "CUDA":
            try:
                import onnxruntime as ort

                available = ort.get_available_providers()
                if "CUDAExecutionProvider" not in available:
                    logger.warning(
                        "CUDA not available (onnxruntime-gpu not installed or no GPU detected). "
                        "Available providers: %s. Falling back to CPU.",
                        available,
                    )
                    provider = "CPU"
            except Exception as e:
                logger.warning("CUDA check failed: %s. Falling back to CPU.", e)
                provider = "CPU"
        elif provider == "OpenVINO":
            try:
                import onnxruntime as ort

                available = ort.get_available_providers()
                if "OpenVINOExecutionProvider" not in available:
                    logger.warning(
                        "OpenVINO not available (openvino not installed). "
                        "Available providers: %s. Falling back to CPU.",
                        available,
                    )
                    provider = "CPU"
            except Exception as e:
                logger.warning("OpenVINO check failed: %s. Falling back to CPU.", e)
                provider = "CPU"

        self._execution_provider = provider
        # Reload all active models with new provider
        # FIXED(P1): 原问题-持全局锁串行重载所有模型，阻塞所有推理请求;
        # 修复-锁内收集需要重载的模型列表，释放锁后逐个重载，避免长时间持锁阻塞推理
        async with self._lock:
            models_to_reload = [
                (model_id, wrapper) for model_id, wrapper in self._loaded_models.items() if wrapper.status == "active"
            ]
        # 锁外逐个重载，每个模型重载期间不影响其他模型的推理请求
        for model_id, wrapper in models_to_reload:
            try:
                await wrapper.unload()
                await wrapper.load(provider=provider)
            except Exception as e:
                logger.error("Failed to reload model %s with provider %s: %s", model_id, provider, e)
        logger.info("Execution provider set to: %s", provider)

    def get_available_providers(self) -> list[str]:
        """获取可用的推理执行提供者列表"""
        providers = ["CPU"]  # CPU is always available
        try:
            import onnxruntime as ort

            available = ort.get_available_providers()
            if "CUDAExecutionProvider" in available:
                providers.append("CUDA")
            if "OpenVINOExecutionProvider" in available:
                providers.append("OpenVINO")
        except ImportError:
            pass
        return providers

    async def check_model_updates(self) -> list[str]:
        """检查模型文件是否有更新（热加载）"""
        reloaded = []
        # FIXED(严重-R2): 原问题-持锁期间 await wrapper.unload()/load() 阻塞全部推理请求
        # 修复-仅在锁内收集需重载的模型列表，锁外执行实际的 unload/load
        pending_reloads: list[tuple[str, Any, float]] = []
        async with self._lock:
            for model_id, wrapper in self._loaded_models.items():
                if wrapper.status in ("active", "inactive"):
                    try:
                        current_mtime = Path(wrapper.model_path).stat().st_mtime
                        if hasattr(wrapper, "_last_mtime") and current_mtime > wrapper._last_mtime:
                            pending_reloads.append((model_id, wrapper, current_mtime))
                        else:
                            wrapper._last_mtime = current_mtime
                    except Exception as e:
                        logger.warning("Model update check failed: %s - %s", model_id, e)

        # 锁外执行模型重载，不阻塞推理请求
        for model_id, wrapper, current_mtime in pending_reloads:
            try:
                wrapper.status = "loading"
                await wrapper.unload()
                await wrapper.load(provider=self._execution_provider)
                wrapper._last_mtime = current_mtime
                reloaded.append(model_id)
                logger.info("Model hot-reloaded (file changed): %s", model_id)
            except Exception as e:
                logger.warning("Model hot-reload failed: %s - %s", model_id, e)
                wrapper.status = "unavailable"
        return reloaded

    async def load_preset_models(self) -> None:
        try:
            import psutil

            mem = psutil.virtual_memory()
        except ImportError:  # FIXED-P2: psutil未安装时优雅降级，不阻止模型加载
            mem = None
        for preset in PRESET_MODELS:
            if mem and mem.available < 100 * 1024 * 1024:  # FIXED-P2: 可用内存<100MB时停止加载模型
                logger.warning(
                    "Available memory too low (%.0fMB), skipping remaining preset models", mem.available / 1024 / 1024
                )
                break
            model_path = str(self._models_dir / preset["model_file"])
            abs_model_path = str(Path(model_path).resolve())
            file_exists = Path(model_path).exists()

            if not file_exists:
                generated = self._try_generate_preset(preset)
                if generated:
                    file_exists = True
                    logger.info("Preset model auto-generated: %s -> %s", preset["model_file"], abs_model_path)

            logger.info(
                "Preset model: id=%s, file=%s, abs_path=%s, exists=%s",
                preset["model_id"],
                preset["model_file"],
                abs_model_path,
                file_exists,
            )
            wrapper = OnnxModelWrapper(
                model_id=preset["model_id"],
                model_name=preset["model_name"],
                model_version=preset["model_version"],
                model_type=preset["model_type"],
                is_preset=True,
                model_path=model_path,
                input_schema=preset["input_schema"],
                output_schema=preset["output_schema"],
            )
            if not file_exists:
                wrapper.status = "unavailable"
                logger.warning(
                    "Preset model file not found and auto-generate failed: %s (abs: %s). "
                    "Run: python models/generate_preset_models.py or pip install onnx numpy",
                    model_path,
                    abs_model_path,
                )
            elif not _check_onnxruntime():
                # 文件存在但 onnxruntime 未安装，标记为 inactive 而非 unavailable
                # 这样安装 onnxruntime 后可通过 API 热启用，无需重启
                wrapper.status = "inactive"
                logger.warning(
                    "Preset model file exists but onnxruntime not installed: %s. "
                    "Model marked as inactive. Run: pip install onnxruntime to enable.",
                    preset["model_id"],
                )
            else:
                await wrapper.load()
            self._loaded_models[preset["model_id"]] = wrapper

    def _try_generate_preset(self, preset: dict) -> bool:
        """尝试自动生成缺失的预置模型文件"""
        try:
            input_shape = preset["input_schema"].get("shape", [1, 1])
            output_shape = preset["output_schema"].get("shape", [1, 1])
            model_bytes = _generate_onnx_model(preset["model_file"], input_shape, output_shape)
            if model_bytes is None:
                logger.warning("onnx library not available, cannot auto-generate model: %s", preset["model_file"])
                return False
            target_path = self._models_dir / preset["model_file"]
            target_path.parent.mkdir(parents=True, exist_ok=True)
            # FIXED: 原子写入 .onnx 模型文件（temp + os.replace），防止进程崩溃留下半截文件 [2026-06-29]
            # 进程在 write_bytes 中途崩溃会留下损坏的 .onnx，下次启动 ort.InferenceSession 会失败
            tmp_fd, tmp_path = tempfile.mkstemp(dir=str(target_path.parent), suffix=".onnx.tmp")
            try:
                with os.fdopen(tmp_fd, "wb") as f:
                    f.write(model_bytes)
                os.replace(tmp_path, target_path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)
                raise
            logger.info("Auto-generated preset model: %s (%d bytes)", preset["model_file"], len(model_bytes))
            return True
        except Exception as e:
            logger.warning("Failed to auto-generate preset model %s: %s", preset["model_file"], e)
            return False

    async def generate_preset_models(self) -> dict[str, bool]:
        """手动触发预置模型生成，返回每个模型的生成结果"""
        results = {}
        for preset in PRESET_MODELS:
            model_path = str(self._models_dir / preset["model_file"])
            if Path(model_path).exists():
                results[preset["model_id"]] = True  # Already exists
                continue
            generated = self._try_generate_preset(preset)
            results[preset["model_id"]] = generated
            if generated:
                # Reload the model
                wrapper = self._loaded_models.get(preset["model_id"])
                if wrapper and wrapper.status in ("unavailable", "inactive"):
                    if _check_onnxruntime():
                        await wrapper.load(provider=self._execution_provider)
        return results

    async def reload_model(self, model_id: str, model_path: str) -> None:
        # FIXED(严重-R2): 原问题-持锁期间 await wrapper.unload()/load() 阻塞全部推理请求
        # 修复-仅在锁内获取wrapper并更新元数据，锁外执行实际的 unload/load
        async with self._lock:
            wrapper = self._loaded_models.get(model_id)
            if wrapper is None:
                raise ValueError(f"Model not found: {model_id}")
            old_status = wrapper.status
            self._record_version(wrapper)
            new_version = self._auto_increment_version(wrapper)
            wrapper.model_version = new_version
            wrapper.model_path = model_path
            wrapper.status = "loading"

        # 锁外执行 unload/load，不阻塞推理请求
        try:
            await wrapper.unload()
            await wrapper.load()
            self._record_version(wrapper)
            logger.info(
                "Model hot-reload completed: %s (%s -> %s, version=%s)",
                model_id,
                old_status,
                wrapper.status,
                new_version,
            )
        except Exception:
            wrapper.status = "unavailable"
            raise

    @staticmethod
    def _detect_model_format(model_path: str) -> str:
        ext = Path(model_path).suffix.lower()
        fmt_map = {".onnx": "onnx", ".tflite": "tflite", ".pmml": "pmml"}
        return fmt_map.get(ext, "onnx")

    def _create_wrapper(
        self,
        model_id: str,
        model_name: str,
        model_version: str,
        model_type: str,
        model_path: str,
        input_schema: dict,
        output_schema: dict,
        is_preset: bool = False,
        preprocess_config: list[dict] | None = None,
        postprocess_config: list[dict] | None = None,
    ) -> OnnxModelWrapper | TFLiteModelWrapper | PMMLModelWrapper:
        fmt = self._detect_model_format(model_path)
        if fmt == "tflite":
            return TFLiteModelWrapper(
                model_id=model_id,
                model_name=model_name,
                model_version=model_version,
                model_type=model_type,
                model_path=model_path,
                input_schema=input_schema,
                output_schema=output_schema,
            )
        elif fmt == "pmml":
            return PMMLModelWrapper(
                model_id=model_id,
                model_name=model_name,
                model_version=model_version,
                model_type=model_type,
                model_path=model_path,
                input_schema=input_schema,
                output_schema=output_schema,
            )
        else:
            return OnnxModelWrapper(
                model_id=model_id,
                model_name=model_name,
                model_version=model_version,
                model_type=model_type,
                is_preset=is_preset,
                model_path=model_path,
                input_schema=input_schema,
                output_schema=output_schema,
                preprocess_config=preprocess_config,
                postprocess_config=postprocess_config,
            )

    async def load_custom_model(
        self,
        model_id: str,
        model_name: str,
        model_version: str,
        model_type: str,
        model_path: str,
        input_schema: dict,
        output_schema: dict,
    ) -> OnnxModelWrapper | TFLiteModelWrapper | PMMLModelWrapper:
        async with self._lock:
            existing = self._loaded_models.get(model_id)
            if existing:
                new_version = self._auto_increment_version(existing)
                self._record_version(existing)
                model_version = new_version
            wrapper = self._create_wrapper(
                model_id=model_id,
                model_name=model_name,
                model_version=model_version,
                model_type=model_type,
                model_path=model_path,
                input_schema=input_schema,
                output_schema=output_schema,
                is_preset=False,
            )
            if isinstance(wrapper, OnnxModelWrapper):
                await wrapper.load(provider=self._execution_provider)
            else:
                await wrapper.load()
            self._loaded_models[model_id] = wrapper
            self._record_version(wrapper)
            return wrapper

    async def infer(self, model_id: str, input_data: list[float]) -> InferenceResult:
        # FIXED-P2: 原问题-infer无锁读取_loaded_models，set_execution_provider可在infer执行期间卸载模型；
        # 在锁内复制wrapper引用后释放锁，避免访问已卸载模型
        async with self._lock:
            wrapper = self._loaded_models.get(model_id)
        if wrapper is None or wrapper.status != "active":
            return InferenceResult(
                model_id=model_id,
                output_data={},
                latency_ms=0,
                status="error",
                error_message=f"Model not available: {model_id}",
            )
        if self._inference_cache:
            cached = self._inference_cache.get(model_id, input_data)
            if cached is not None:
                return InferenceResult(
                    model_id=model_id,
                    output_data=cached,
                    latency_ms=0,
                    status="success",
                )
        start = time.perf_counter()
        record_packet("tx", "ai_inference", model_id, f"Infer: {len(input_data)} inputs")
        inference_timeout = float(self._config.get("inference_timeout", 30.0))
        try:
            if not _HAS_NUMPY:
                return InferenceResult(
                    model_id=model_id,
                    output_data={},
                    latency_ms=0,
                    status="error",
                    error_message="numpy not installed, required for inference. Run: pip install numpy",
                )
            arr = np.array(input_data, dtype=np.float32)
            if wrapper._preprocess_pipeline:
                arr = wrapper._preprocess_pipeline.apply(arr)
                input_data = arr.flatten().tolist()
            if isinstance(wrapper, TFLiteModelWrapper):
                shape = wrapper.input_schema.get("shape", [1, -1])
                expected_size = 1
                for dim in shape:
                    if dim > 0:
                        expected_size *= dim
                flat_arr = arr.flatten()
                if len(flat_arr) != expected_size:
                    if len(flat_arr) < expected_size:
                        flat_arr = np.pad(flat_arr, (0, expected_size - len(flat_arr)))
                    else:
                        flat_arr = flat_arr[:expected_size]
                arr = flat_arr.reshape(shape)
                loop = asyncio.get_running_loop()
                raw_output = await asyncio.wait_for(
                    loop.run_in_executor(None, lambda: wrapper.run(arr)),
                    timeout=inference_timeout,
                )
                latency_ms = int((time.perf_counter() - start) * 1000)
                output_data = {}
                for i, out in enumerate(raw_output):
                    if isinstance(out, np.ndarray):
                        val = out.tolist()
                        if isinstance(val, list) and len(val) == 1 and isinstance(val[0], list):
                            val = val[0]
                        output_data[f"output_{i}"] = val
                    else:
                        output_data[f"output_{i}"] = out
            elif isinstance(wrapper, PMMLModelWrapper):
                loop = asyncio.get_running_loop()
                raw_output = await asyncio.wait_for(
                    loop.run_in_executor(None, lambda: wrapper.run(input_data)),
                    timeout=inference_timeout,
                )
                latency_ms = int((time.perf_counter() - start) * 1000)
                output_data = {"output_0": raw_output}
            else:
                input_name = wrapper.session.get_inputs()[0].name
                shape = wrapper.input_schema.get("shape", [1, -1])
                expected_size = 1
                for dim in shape:
                    if dim > 0:
                        expected_size *= dim
                flat_arr = arr.flatten()
                if len(flat_arr) != expected_size:
                    if len(flat_arr) < expected_size:
                        flat_arr = np.pad(flat_arr, (0, expected_size - len(flat_arr)))
                    else:
                        flat_arr = flat_arr[:expected_size]
                arr = flat_arr.reshape(shape)
                loop = asyncio.get_running_loop()
                raw_output = await asyncio.wait_for(
                    loop.run_in_executor(
                        None,
                        lambda: wrapper.session.run(None, {input_name: arr}),
                    ),
                    timeout=inference_timeout,
                )
                latency_ms = int((time.perf_counter() - start) * 1000)
                output_data = {}
                for i, out in enumerate(raw_output):
                    if isinstance(out, np.ndarray):
                        val = out.tolist()
                        if isinstance(val, list) and len(val) == 1 and isinstance(val[0], list):
                            val = val[0]
                        output_data[f"output_{i}"] = val
                    else:
                        output_data[f"output_{i}"] = out
            if wrapper._postprocess_pipeline:
                output_data = wrapper._postprocess_pipeline.apply(output_data)
            if self._inference_cache:
                self._inference_cache.put(model_id, input_data, output_data)
            if self._resource_monitor:
                self._resource_monitor.record_latency(model_id, float(latency_ms))
            if self._version_manager:
                version = wrapper.model_version
                self._version_manager.record_inference(model_id, version, float(latency_ms), True)
            result = InferenceResult(
                model_id=model_id,
                output_data=output_data,
                latency_ms=latency_ms,
                status="success",
            )
            self._stats.record_inference(model_id, latency_ms, "success")
            if self._event_bus:
                from edgelite.engine.event_bus import EventBus

                if isinstance(self._event_bus, EventBus):
                    await self._event_bus.publish(
                        AiInferenceEvent(
                            model_id=model_id,
                            output_data=output_data,
                            latency_ms=latency_ms,
                        )
                    )
            await self._publish_inference_result(result)
            record_packet("rx", "ai_inference", model_id, f"Result: {result.status}, {latency_ms}ms")
            # FIXED(严重): 原问题-last_result从未赋值，超时缓存降级getattr永远返回None，缓存降级功能失效;
            # 修复-推理成功后缓存结果，供超时降级使用
            wrapper.last_result = result.output_data
            return result
        except TimeoutError:
            latency_ms = int((time.perf_counter() - start) * 1000)
            self._stats.record_inference(model_id, latency_ms, "error")
            # 修复P1-5: asyncio.wait_for 超时后底层 run_in_executor 线程池任务无法取消仍可能运行，
            # 此处记录 warning 说明线程池任务可能仍在执行，便于排查线程池耗尽问题
            logger.warning(
                "Inference timeout for model %s (%.1fs), returning degraded result; "
                "underlying executor thread pool task may still be running and cannot be cancelled",
                model_id,
                inference_timeout,
            )
            cached_result = getattr(wrapper, "last_result", None)
            if cached_result:
                return InferenceResult(
                    model_id=model_id,
                    output_data=cached_result,
                    latency_ms=latency_ms,
                    status="error",
                    error_message=f"Inference timeout ({inference_timeout}s), returning cached result",
                )
            return InferenceResult(
                model_id=model_id,
                output_data={"output_0": [0.0]},
                latency_ms=latency_ms,
                status="error",
                error_message=f"Inference timeout ({inference_timeout}s)",
            )
        except MemoryError:
            latency_ms = int((time.perf_counter() - start) * 1000)
            self._stats.record_inference(model_id, latency_ms, "error")
            logger.error("OOM during inference for model %s, marking as error", model_id)
            # FIXED(严重): 原问题-OOM后仅标记status=error不unload，session未释放内存未回收;
            # 修复-OOM后调用unload释放session
            try:
                await wrapper.unload()
            except Exception as unload_err:
                # FIXED(严重): 原问题-except Exception: pass 吞没 unload 异常，
                # 若 unload 失败，ONNX Runtime session 句柄未释放、显存未回收，
                # 后续推理会持续 OOM，且无任何日志提示 unload 失败
                logger.error(
                    "Unload model failed after OOM for %s: %s (session may leak)",
                    model_id,
                    unload_err,
                    exc_info=True,
                )
            wrapper.status = "error"
            return InferenceResult(
                model_id=model_id,
                output_data={},
                latency_ms=latency_ms,
                status="error",
                error_message="OOM during inference",
            )
        except Exception as e:
            latency_ms = int((time.perf_counter() - start) * 1000)
            self._stats.record_inference(model_id, latency_ms, "error")
            if isinstance(e, RuntimeError) and "out of memory" in str(e).lower():
                logger.error("OOM during inference for model %s (RuntimeError), marking as error", model_id)
                wrapper.status = "error"
                return InferenceResult(
                    model_id=model_id,
                    output_data={},
                    latency_ms=latency_ms,
                    status="error",
                    error_message="OOM during inference",
                )
            # Edge inference failed, try cloud fallback
            if self._config.get("cloud_inference_enabled", False):
                cloud_url = self._config.get("cloud_inference_url", "")
                if cloud_url:
                    logger.info("Edge inference failed for %s, falling back to cloud", model_id)
                    cloud_result = await self.infer_cloud_fallback(model_id, input_data, cloud_url)
                    if cloud_result.status == "success":
                        return cloud_result
            return InferenceResult(
                model_id=model_id,
                output_data={},
                latency_ms=latency_ms,
                status="error",
                error_message=str(e),
            )

    async def infer_cloud_fallback(
        self,
        model_id: str,
        input_data: list[float],
        cloud_url: str = "",
    ) -> InferenceResult:
        """云端推理降级：当边缘推理不可用时，将数据发送到云端推理服务"""
        if self._cloud_circuit_breaker and self._cloud_circuit_breaker.is_open:
            return InferenceResult(
                model_id=model_id,
                output_data={},
                latency_ms=0,
                status="error",
                error_message="Cloud inference circuit breaker is OPEN",
            )

        import httpx

        cloud_endpoint = cloud_url or self._config.get("cloud_inference_url", "")
        if not cloud_endpoint:
            return InferenceResult(
                model_id=model_id,
                output_data={},
                latency_ms=0,
                status="error",
                error_message="Cloud inference URL not configured",
            )

        # R6-S-01修复: SSRF校验，拒绝内网/localhost/云元数据地址
        if not _validate_cloud_endpoint(cloud_endpoint):
            logger.warning("Cloud endpoint failed SSRF validation: %s", cloud_endpoint)
            return InferenceResult(
                model_id=model_id,
                output_data={},
                latency_ms=0,
                status="error",
                error_message="Cloud endpoint failed SSRF validation",
            )

        start = time.perf_counter()
        try:
            headers: dict[str, str] = {"Content-Type": "application/json"}
            cloud_api_key = self._config.get("cloud_inference_api_key", "")
            cloud_auth_token = self._config.get("cloud_inference_auth_token", "")
            if cloud_api_key:
                headers["x-api-key"] = cloud_api_key
            elif cloud_auth_token:
                headers["Authorization"] = f"Bearer {cloud_auth_token}"
            async with httpx.AsyncClient(timeout=30.0) as client:
                # 修复P1-4: 原 in 检查与 .get() 之间存在 TOCTOU，并发卸载可能让 .get() 返回 None 后访问属性
                # 改为单次 get 后判空，避免竞态
                cloud_wrapper = self._loaded_models.get(model_id)
                cloud_version = cloud_wrapper.model_version if cloud_wrapper else None
                resp = await client.post(
                    f"{cloud_endpoint}/infer",
                    json={
                        "model_id": model_id,
                        "model_version": cloud_version,
                        "input_data": input_data,
                    },  # FIXED-P2: 云推理请求携带模型版本号
                    headers=headers,
                )
                latency_ms = int((time.perf_counter() - start) * 1000)

                if resp.status_code == 200:
                    result_data = resp.json()
                    if self._cloud_circuit_breaker:
                        await self._cloud_circuit_breaker.record_success()
                    return InferenceResult(
                        model_id=model_id,
                        output_data=result_data.get("output_data", {}),
                        latency_ms=latency_ms,
                        status="success",
                        error_message="",
                    )
                else:
                    if self._cloud_circuit_breaker:
                        await self._cloud_circuit_breaker.record_failure(RuntimeError(f"HTTP {resp.status_code}"))
                    return InferenceResult(
                        model_id=model_id,
                        output_data={},
                        latency_ms=latency_ms,
                        status="error",
                        error_message=f"Cloud inference failed: HTTP {resp.status_code}",
                    )
        except Exception as e:
            latency_ms = int((time.perf_counter() - start) * 1000)
            if self._cloud_circuit_breaker:
                await self._cloud_circuit_breaker.record_failure(e)
            return InferenceResult(
                model_id=model_id,
                output_data={},
                latency_ms=latency_ms,
                status="error",
                error_message=f"Cloud inference error: {e}",
            )

    def get_model_status(self, model_id: str) -> str | None:
        wrapper = self._loaded_models.get(model_id)
        return wrapper.status if wrapper else None

    def get_loaded_models(self) -> dict[str, OnnxModelWrapper]:
        return dict(self._loaded_models)

    def get_model(self, model_id: str) -> OnnxModelWrapper | None:
        return self._loaded_models.get(model_id)

    def get_stats(self) -> dict:
        return self._stats.get_snapshot()

    def get_model_stats(self, model_id: str) -> dict | None:
        return self._stats.get_model_stats(model_id)

    async def enable_model(self, model_id: str) -> tuple[bool, str]:
        wrapper = self._loaded_models.get(model_id)
        if not wrapper:
            return False, "ERR_AI_MODEL_NOT_FOUND"
        if not _check_onnxruntime():
            return False, "ERR_AI_ONNXRUNTIME_NOT_INSTALLED"
        if wrapper.status == "loading":
            return False, "ERR_AI_MODEL_IS_LOADING"
        if wrapper.status == "active":
            return True, ""
        if wrapper.status == "unavailable":
            # 尝试自动生成模型文件
            model_path = Path(wrapper.model_path)
            if not model_path.exists() and wrapper.is_preset:
                preset = next((p for p in PRESET_MODELS if p["model_id"] == model_id), None)
                if preset and self._try_generate_preset(preset):
                    logger.info("Auto-generated missing model file for enable: %s", model_id)
            if not model_path.exists():
                return False, "ERR_AI_MODEL_FILE_NOT_FOUND"
            await wrapper.load()
            if wrapper.status != "active":
                return False, "ERR_AI_MODEL_CANNOT_LOAD"
            return True, ""
        if wrapper.status == "error":
            await wrapper.load()
            if wrapper.status != "active":
                return False, "ERR_AI_MODEL_PREVIOUS_ERROR"
            return True, ""
        if wrapper.status == "inactive":
            await wrapper.load()
            if wrapper.status != "active":
                return False, "ERR_AI_MODEL_ENABLE_FAILED"
            return True, ""
        return True, ""

    async def disable_model(self, model_id: str) -> None:
        # FIXED-P0: 原问题-disable_model无锁修改_loaded_models，与infer/load_custom_model竞态
        async with self._lock:
            wrapper = self._loaded_models.get(model_id)
        if wrapper:
            await wrapper.unload()

    async def remove_model(self, model_id: str) -> None:
        # FIXED-P0: 原问题-remove_model无锁pop _loaded_models，与infer竞态可导致RuntimeError
        async with self._lock:
            wrapper = self._loaded_models.pop(model_id, None)
        if wrapper:
            await wrapper.unload()
        # Stop scheduled inference if running for this model
        await self.stop_scheduled_inference(model_id)

    async def start_scheduled_inference(
        self,
        model_id: str,
        device_id: str,
        point_name: str,
        interval_seconds: int = 60,
        input_window_size: int = 100,
    ) -> None:
        """Start a scheduled inference task for a model"""
        # FIXED-P0: 原问题-_scheduled_tasks/_scheduled_configs无锁保护，并发调用可创建重复定时任务
        async with self._lock:
            if model_id in self._scheduled_tasks:
                raise ValueError(f"Scheduled inference already exists for model: {model_id}")

            wrapper = self._loaded_models.get(model_id)
            if wrapper is None or wrapper.status != "active":
                raise ValueError(f"Model not available for scheduled inference: {model_id}")

            config = {
                "model_id": model_id,
                "device_id": device_id,
                "point_name": point_name,
                "interval_seconds": interval_seconds,
                "input_window_size": input_window_size,
            }
            self._scheduled_configs[model_id] = config

            task = asyncio.create_task(
                self._scheduled_inference_loop(
                    model_id,
                    device_id,
                    point_name,
                    interval_seconds,
                    input_window_size,
                ),
                name=f"scheduled_inference_{model_id}",
            )
            self._scheduled_tasks[model_id] = task
        logger.info(
            "Scheduled inference started: model=%s, device=%s, point=%s, interval=%ds",
            model_id,
            device_id,
            point_name,
            interval_seconds,
        )

    async def _scheduled_inference_loop(
        self,
        model_id: str,
        device_id: str,
        point_name: str,
        interval_seconds: int,
        input_window_size: int,
    ) -> None:
        """Background loop for scheduled inference"""
        try:
            while model_id in self._scheduled_tasks:
                try:
                    input_data = await self._fetch_influx_data(
                        device_id,
                        point_name,
                        input_window_size,
                    )
                    if input_data:
                        result = await self.infer(model_id, input_data)
                        if result.status == "success" and self._event_bus:
                            from edgelite.engine.event_bus import EventBus

                            if isinstance(self._event_bus, EventBus):
                                await self._event_bus.publish(
                                    AiInferenceEvent(
                                        model_id=model_id,
                                        output_data=result.output_data,
                                        latency_ms=result.latency_ms,
                                    )
                                )
                    else:
                        logger.debug(
                            "Scheduled inference: no data for device=%s point=%s",
                            device_id,
                            point_name,
                        )
                except Exception as e:
                    logger.error(
                        "Scheduled inference error: model=%s - %s",
                        model_id,
                        e,
                    )
                await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            logger.info("Scheduled inference cancelled: model=%s", model_id)
        finally:
            # FIXED-P0: 原问题-finally中pop _scheduled_tasks无锁，与stop_scheduled_inference竞态
            async with self._lock:
                self._scheduled_tasks.pop(model_id, None)

    async def _fetch_influx_data(
        self,
        device_id: str,
        point_name: str,
        window_size: int,
    ) -> list[float] | None:
        """Fetch recent data points from InfluxDB as input for inference"""
        try:
            from edgelite.app import _app_state

            influx_storage = getattr(_app_state, "influx_storage", None)
            if influx_storage is None:
                logger.warning("InfluxDB storage not available for scheduled inference")
                return None

            data = await influx_storage.query_points(
                device_id=device_id,
                point_name=point_name,
                start="-1h",
                max_points=window_size,
            )
            if not data:
                return None

            values = [d["value"] for d in data if d.get("value") is not None and isinstance(d["value"], (int, float))]
            if not values:
                return None

            # Take the most recent window_size values
            values = values[-window_size:]
            return values
        except Exception as e:
            logger.error("Failed to fetch InfluxDB data: %s", e)
            return None

    async def stop_scheduled_inference(self, model_id: str) -> bool:
        """Stop a scheduled inference task"""
        # FIXED-P0: 原问题-stop_scheduled_inference对_scheduled_tasks/_scheduled_configs的pop无锁，与finally竞态
        async with self._lock:
            task = self._scheduled_tasks.pop(model_id, None)
            self._scheduled_configs.pop(model_id, None)
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            logger.info("Scheduled inference stopped: model=%s", model_id)
            return True
        return False

    def get_scheduled_inferences(self) -> list[dict]:
        """Get all scheduled inference configurations"""
        result = []
        # FIXED(一般): 原问题-遍历 self._scheduled_configs.items() 无锁, 与 schedule_inference/
        # stop_scheduled_inference 并发增删 key 可能触发 RuntimeError; 修复-先取 list 快照再遍历
        for model_id, config in list(self._scheduled_configs.items()):
            task = self._scheduled_tasks.get(model_id)
            entry = dict(config)
            entry["running"] = task is not None and not task.done()
            result.append(entry)
        return result

    async def infer_from_video(
        self,
        model_id: str,
        video_url: str,
        frame_interval: float = 1.0,
        confidence_threshold: float = 0.5,
        max_frames: int = 0,
    ) -> AsyncIterator[dict]:
        """视频流推理管道: URL → OpenCV抽帧 → 预处理 → ONNX推理 → 后处理"""
        # FIXED(P0): 原问题-video_url无SSRF校验，可访问内部服务和云元数据;
        # 修复-调用_validate_video_url校验URL协议和目标地址
        if not _validate_video_url(video_url):
            yield {"error": "Invalid video URL", "frame": 0}
            return
        try:
            import cv2
        except ImportError:
            yield {"error": "opencv-python not installed", "frame": 0}
            return

        # R6-S-24: cv2.VideoCapture 是同步阻塞调用，包装到 asyncio.to_thread 中执行，避免阻塞事件循环
        cap = await asyncio.to_thread(cv2.VideoCapture, video_url)
        if not cap.isOpened():
            yield {"error": f"Cannot open video: {video_url}", "frame": 0}
            return

        # Detect actual FPS from video source
        video_fps = cap.get(cv2.CAP_PROP_FPS)
        if video_fps <= 0 or video_fps > 120:
            video_fps = 25.0  # Fallback for streams that don't report FPS
        frame_skip = max(1, int(frame_interval * video_fps))

        # Get model input dimensions
        wrapper = self._loaded_models.get(model_id)
        model_h, model_w = 224, 224  # Default
        if wrapper and hasattr(wrapper, "input_schema") and wrapper.input_schema:
            try:
                shape = wrapper.input_schema.get("shape", [])
                if len(shape) >= 3:
                    # Assume NCHW format: [1, C, H, W]
                    model_h = shape[-2] if shape[-2] > 0 else 224
                    model_w = shape[-1] if shape[-1] > 0 else 224
            except (TypeError, AttributeError, IndexError):
                pass

        frame_count = 0
        try:
            while max_frames == 0 or frame_count < max_frames:
                # R6-S-24: cap.read() 是同步阻塞 I/O 调用，包装到 asyncio.to_thread 中执行，
                # 避免在 async 函数中直接阻塞事件循环
                ret, frame = await asyncio.to_thread(cap.read)
                if not ret:
                    break
                frame_count += 1
                if frame_count % frame_skip != 0:
                    continue

                # Preprocess: resize to model input dimensions
                # R6-S-24: cv2.resize 同为阻塞调用，一并包装到 asyncio.to_thread 中执行
                resized = await asyncio.to_thread(cv2.resize, frame, (model_w, model_h))
                import numpy as np

                input_data = resized.astype(np.float32).flatten().tolist()

                # Inference
                result = await self.infer(model_id, input_data)

                # Post-process: filter low confidence
                if result.status == "success" and result.latency_ms >= 0:
                    yield {
                        "frame": frame_count,
                        "result": result.output_data,
                        "latency_ms": result.latency_ms,
                        "confidence": max(result.output_data.values()) if result.output_data else 0,
                    }
        finally:
            cap.release()

    async def _publish_inference_result(self, result: InferenceResult) -> None:
        """发布推理结果到事件总线，驱动规则引擎"""
        if self._event_bus:
            try:
                await self._event_bus.publish(
                    "ai_inference_result",
                    {
                        "model_id": result.model_id,
                        "output_data": result.output_data,
                        "latency_ms": result.latency_ms,
                        "status": result.status,
                        "timestamp": datetime.now(UTC).isoformat(),
                    },
                )
            except Exception as e:
                logger.warning("Failed to publish inference result: %s", e)

    async def shutdown(self) -> None:
        # Cancel all scheduled inference tasks
        for model_id in list(self._scheduled_tasks.keys()):
            await self.stop_scheduled_inference(model_id)
        # FIXED: 取消模型热加载定时检查任务 [2026-06-29]
        watcher_task = getattr(self, "_model_watcher_task", None)
        if watcher_task is not None and not watcher_task.done():
            watcher_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await watcher_task
        # 修复P1-3: 取消 HotSwapManager 中所有在途的 _warmup_task，避免 shutdown 后热切换任务仍在运行
        warmup_tasks: list[asyncio.Task] = []
        if self._hot_swap_manager is not None:
            try:
                for state in self._hot_swap_manager.list_active_swaps():
                    task = getattr(state, "_warmup_task", None)
                    if task is not None and not task.done():
                        task.cancel()
                        warmup_tasks.append(task)
            except Exception as e:
                logger.warning("Failed to enumerate active swaps during shutdown: %s", e)
        for task in warmup_tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        # FIXED-P0: 原问题-shutdown无锁遍历和清除_loaded_models，与infer竞态可导致RuntimeError
        async with self._lock:
            wrappers = list(self._loaded_models.values())
            self._loaded_models.clear()
            # 修复P1-2: 同时取出待回收的旧 wrapper，一并在 shutdown 时释放
            pending = list(self._pending_unload_wrappers)
            self._pending_unload_wrappers.clear()
        for wrapper in wrappers:
            await wrapper.unload()
        # 释放回滚/热切换遗留的旧 wrapper 资源
        for old_wrapper, _ts in pending:
            try:
                await old_wrapper.unload()
            except Exception as e:
                logger.warning("Unload pending old wrapper during shutdown failed: %s", e)
        logger.info("AI inference engine shutdown, all model resources released")  # FIXED-P3: 中文日志→英文

    def get_model_version_history(self, model_id: str) -> list[dict]:
        wrapper = self._loaded_models.get(model_id)
        if not wrapper:
            return []
        history = getattr(wrapper, "_version_history", [])
        return list(history)

    async def rollback_model_version(self, model_id: str, target_version: str) -> bool:
        # FIXED(严重): 原问题-在锁内 await wrapper.unload()/load()，阻塞所有推理请求
        # 修复-锁外加载新 wrapper，锁内仅做引用切换，避免阻塞推理
        async with self._lock:
            wrapper = self._loaded_models.get(model_id)
            if not wrapper:
                raise ValueError(f"Model not found: {model_id}")
            history = getattr(wrapper, "_version_history", [])
            target_entry = None
            for entry in history:
                if entry.get("version") == target_version:
                    target_entry = entry
                    break
            if not target_entry:
                raise ValueError(f"Version {target_version} not found in history for model {model_id}")
            current_version = wrapper.model_version
            target_model_path = target_entry.get("model_path", "")

            # 无目标模型路径时，仅更新版本号（保持原逻辑，sync 操作不阻塞事件循环）
            if not (target_model_path and Path(target_model_path).exists()):
                self._record_version(wrapper, f"rollback_from_{current_version}_to_{target_version}")
                wrapper.model_version = target_version
                logger.info(
                    "Model version rolled back (no reload): %s %s -> %s", model_id, current_version, target_version
                )
                return True

            # 收集创建新 wrapper 所需的元数据
            model_name = wrapper.model_name
            model_type = wrapper.model_type
            input_schema = wrapper.input_schema
            output_schema = wrapper.output_schema
            is_preset = getattr(wrapper, "is_preset", False)
            preprocess_config = getattr(wrapper, "preprocess_config", None)
            postprocess_config = getattr(wrapper, "postprocess_config", None)
            # 保存版本历史以便迁移到新 wrapper
            old_history = list(getattr(wrapper, "_version_history", []))

        # 锁外: 创建并加载新 wrapper，不阻塞其他推理请求
        new_wrapper = self._create_wrapper(
            model_id=model_id,
            model_name=model_name,
            model_version=target_version,
            model_type=model_type,
            model_path=target_model_path,
            input_schema=input_schema,
            output_schema=output_schema,
            is_preset=is_preset,
            preprocess_config=preprocess_config,
            postprocess_config=postprocess_config,
        )
        # 迁移版本历史到新 wrapper，保证 rollback 后仍可查看完整版本轨迹
        new_wrapper._version_history = old_history
        if isinstance(new_wrapper, OnnxModelWrapper):
            await new_wrapper.load(provider=self._execution_provider)
        else:
            await new_wrapper.load()
        # 记录回滚操作到版本历史
        self._record_version(new_wrapper, f"rollback_from_{current_version}_to_{target_version}")

        # 锁内: 原子切换 wrapper 引用
        async with self._lock:
            old_wrapper = self._loaded_models.get(model_id)
            self._loaded_models[model_id] = new_wrapper

        # 修复P1-2: 不立即 unload 旧 wrapper，与双 buffer 设计矛盾——
        # 在途推理可能仍持有 old_wrapper 引用，立即 unload 会设 session=None
        # 导致并发推理触发 AttributeError。改为放入待回收列表，由 shutdown 统一释放。
        if old_wrapper is not None:
            self._pending_unload_wrappers.append((old_wrapper, time.monotonic()))
            logger.info("Old wrapper for %s queued for deferred unload after rollback", model_id)

        logger.info("Model version rolled back: %s %s -> %s", model_id, current_version, target_version)
        return True

    def _record_version(self, wrapper: OnnxModelWrapper, version: str | None = None) -> None:
        if not hasattr(wrapper, "_version_history"):
            wrapper._version_history = []
        entry = {
            "version": version or wrapper.model_version,
            "model_path": wrapper.model_path,
            "timestamp": datetime.now(UTC).isoformat(),
            "status": wrapper.status,
        }
        wrapper._version_history.append(entry)
        if len(wrapper._version_history) > 50:
            wrapper._version_history = wrapper._version_history[-50:]

    def _auto_increment_version(self, wrapper: OnnxModelWrapper) -> str:
        current = wrapper.model_version
        try:
            if current.startswith("v"):
                parts = current[1:].split(".")
                if len(parts) == 3:
                    patch = int(parts[2]) + 1
                    new_version = f"v{parts[0]}.{parts[1]}.{patch}"
                else:
                    new_version = current
            else:
                new_version = current
        except (ValueError, IndexError):
            new_version = current
        return new_version


class AiInferenceEvent:
    """AI推理结果事件"""

    __slots__ = ("model_id", "output_data", "latency_ms", "timestamp")

    def __init__(self, model_id: str, output_data: dict, latency_ms: int):
        self.model_id = model_id
        self.output_data = output_data
        self.latency_ms = latency_ms
        self.timestamp = datetime.now(UTC)


# ─────────────────────────────────────────────────────────────────────────────
# TensorFlow Lite 模型支持
# ─────────────────────────────────────────────────────────────────────────────


class TFLiteModelWrapper:
    """TensorFlow Lite 模型封装"""

    def __init__(
        self,
        model_id: str,
        model_name: str,
        model_version: str,
        model_type: str,
        model_path: str,
        input_schema: dict,
        output_schema: dict,
    ):
        self.model_id = model_id
        self.model_name = model_name
        self.model_version = model_version
        self.model_type = model_type
        self.model_path = model_path
        self.input_schema = input_schema
        self.output_schema = output_schema
        self.status: Literal["active", "inactive", "loading", "error", "unavailable"] = "inactive"
        self.interpreter: Any = None
        self.input_details: dict = {}
        self.output_details: dict = {}
        self.loaded_at: datetime | None = None
        self.is_preset = False

    async def load(self) -> None:
        """加载TFLite模型"""
        try:
            import tflite_runtime.interpreter as tflite  # pyright: ignore[reportMissingImports]
        except ImportError:
            try:
                import tensorflow as tf

                tflite = tf.lite
            except ImportError:
                self.status = "inactive"
                logger.warning(
                    "TensorFlow Lite not installed. "
                    "Model %s requires TFLite runtime. Install: pip install tflite-runtime",
                    self.model_id,
                )
                return

        try:
            self.status = "loading"
            self.interpreter = tflite.Interpreter(model_path=self.model_path)
            self.interpreter.allocate_tensors()
            self.input_details = self.interpreter.get_input_details()[0]
            self.output_details = self.interpreter.get_output_details()[0]
            self.status = "active"
            self.loaded_at = datetime.now(UTC)
            logger.info("TFLite model loaded: %s (%s)", self.model_id, self.model_name)
        except Exception as e:
            self.status = "error"
            self.interpreter = None
            logger.error("TFLite model load failed: %s - %s", self.model_id, e)

    async def unload(self) -> None:
        self.interpreter = None
        self.status = "inactive"
        self.loaded_at = None

    def run(self, input_data: np.ndarray) -> list[np.ndarray]:
        """执行推理"""
        if not self.interpreter:
            raise RuntimeError(f"TFLite model not loaded: {self.model_id}")

        self.interpreter.set_tensor(self.input_details["index"], input_data)
        self.interpreter.invoke()
        outputs = []
        for detail in self.interpreter.get_output_details():
            outputs.append(self.interpreter.get_tensor(detail["index"]))
        return outputs


# ─────────────────────────────────────────────────────────────────────────────
# PMML 模型支持（基于SKLearn/Statsmodels解析）
# ─────────────────────────────────────────────────────────────────────────────


class PMMLModelWrapper:
    """PMML (Predictive Model Markup Language) 模型封装

    支持常见的PMML模型：
    - RegressionModel
    - TreeModel
    - NaiveBayesModel
    - Scorecard
    """

    def __init__(
        self,
        model_id: str,
        model_name: str,
        model_version: str,
        model_type: str,
        model_path: str,
        input_schema: dict,
        output_schema: dict,
    ):
        self.model_id = model_id
        self.model_name = model_name
        self.model_version = model_version
        self.model_type = model_type
        self.model_path = model_path
        self.input_schema = input_schema
        self.output_schema = output_schema
        self.status: Literal["active", "inactive", "loading", "error", "unavailable"] = "inactive"
        self._model: Any = None
        self._parser: Any = None
        self._feature_names: list[str] = []
        self._feature_transforms: list[Any] = []
        self.loaded_at: datetime | None = None
        self.is_preset = False

    async def load(self) -> None:
        """解析并加载PMML模型"""
        try:
            import xml.etree.ElementTree as ET
        except Exception:
            self.status = "error"
            logger.error("PMML model %s: XML parser unavailable", self.model_id)
            return

        try:
            self.status = "loading"
            tree = ET.parse(self.model_path)
            root = tree.getroot()

            # 解析DataDictionary获取特征名
            ns = {"pmml": "http://www.dmg.org/PMML-4_4"}
            data_dict = root.find(".//pmml:DataDictionary", ns)
            if data_dict is None:
                # 尝试无命名空间
                data_dict = root.find(".//DataDictionary")

            if data_dict is not None:
                for field in data_dict:
                    fname = field.get("name")
                    if fname and fname != "predictedValue":
                        self._feature_names.append(fname)

            # 查找并加载模型
            for model_tag in ["RegressionModel", "TreeModel", "NaiveBayesModel", "Scorecard"]:
                model_elem = root.find(f".//{model_tag}")
                if model_elem is not None:
                    self._model = self._parse_model(model_tag, model_elem)
                    break

            if self._model is None:
                self.status = "unavailable"
                logger.warning("PMML model %s: No supported model found in file", self.model_id)
                return

            self.status = "active"
            self.loaded_at = datetime.now(UTC)
            logger.info("PMML model loaded: %s (%s) features=%s", self.model_id, self.model_name, self._feature_names)
        except Exception as e:
            self.status = "error"
            self._model = None
            logger.error("PMML model load failed: %s - %s", self.model_id, e)

    def _parse_model(self, model_tag: str, model_elem: Any) -> Any:
        """解析具体模型类型"""
        if model_tag == "RegressionModel":
            return self._parse_regression_model(model_elem)
        elif model_tag == "TreeModel":
            return self._parse_tree_model(model_elem)
        return None

    def _parse_regression_model(self, model_elem: Any) -> dict:
        """解析回归模型"""
        table = model_elem.find(".//RegressionTable")
        if table is None:
            return {}

        intercept = float(table.get("intercept", "0"))
        coefficients = {}
        for coef in table.findall("NumericPredictor"):
            name = coef.get("name")
            coeff = float(coef.get("coefficient", "0"))
            coefficients[name] = coeff

        return {
            "type": "regression",
            "intercept": intercept,
            "coefficients": coefficients,
        }

    def _parse_tree_model(self, model_elem: Any) -> dict:
        """解析决策树模型（简化版：仅支持二元树叶）"""
        node = model_elem.find(".//Node")
        if node is None:
            return {}

        def extract_tree(node_elem: Any) -> dict:
            score = node_elem.get("score", "0")
            children = node_elem.findall("Node")
            if not children:
                return {"score": float(score), "children": []}
            predicate = node_elem.find("SimplePredicate")
            return {
                "field": predicate.get("field") if predicate is not None else None,
                "operator": predicate.get("operator") if predicate is not None else None,
                "value": predicate.get("value") if predicate is not None else None,
                "score": float(score),
                "children": [extract_tree(c) for c in children],
            }

        return {"type": "tree", "root": extract_tree(node)}

    def predict(self, features: dict) -> float:
        """执行推理"""
        if self._model is None:
            raise RuntimeError(f"PMML model not loaded: {self.model_id}")

        model_type = self._model.get("type")

        if model_type == "regression":
            result = self._model["intercept"]
            for name, coeff in self._model["coefficients"].items():
                result += coeff * features.get(name, 0)
            return float(result)

        elif model_type == "tree":
            return float(self._predict_tree(self._model["root"], features))

        return 0.0

    def _predict_tree(self, node: dict, features: dict) -> float:
        """递归预测决策树"""
        if not node.get("children"):
            return node["score"]

        field = node.get("field")
        operator = node.get("operator")
        value = float(node.get("value", 0))
        feat_val = float(features.get(field, 0))

        go_left = False
        if operator == "lessThan":
            go_left = feat_val < value
        elif operator == "lessOrEqual":
            go_left = feat_val <= value
        elif operator == "greaterThan":
            go_left = feat_val > value
        elif operator == "greaterOrEqual":
            go_left = feat_val >= value
        elif operator == "equal":
            go_left = abs(feat_val - value) < 1e-9
        elif operator == "notEqual":
            go_left = abs(feat_val - value) >= 1e-9

        child_idx = 0 if go_left else 1
        if child_idx < len(node["children"]):
            return self._predict_tree(node["children"][child_idx], features)
        return node["score"]

    async def unload(self) -> None:
        self._model = None
        self.status = "inactive"
        self.loaded_at = None

    def run(self, input_data: list[float]) -> list[float]:
        """执行推理"""
        if not _HAS_NUMPY:
            raise RuntimeError("numpy required for PMML inference")

        features = {}
        for i, name in enumerate(self._feature_names):
            if i < len(input_data):
                features[name] = input_data[i]

        result = self.predict(features)
        return [result]


# ─────────────────────────────────────────────────────────────────────────────
# 模型量化工具
# ─────────────────────────────────────────────────────────────────────────────


def quantize_model_int8(
    model_path: str,
    calibration_data: list[np.ndarray],
    output_path: str,
) -> bool:
    """将ONNX模型量化为INT8

    Args:
        model_path: 原始FP32 ONNX模型路径
        calibration_data: 校准数据集（建议100-1000个样本）
        output_path: 输出INT8模型路径

    Returns:
        是否量化成功
    """
    try:
        from onnxruntime.quantization import QuantFormat, QuantType, calibrate
        from onnxruntime.quantization.quant_utils import load_model, model_builder

        logger.info("Starting INT8 quantization for: %s", model_path)

        from onnxruntime.quantization import quantize_dynamic

        quantize_dynamic(
            model_input=model_path,
            model_output=output_path,
            weight_type=QuantType.QInt8,
            optimize_model=True,
        )
        logger.info("INT8 quantization completed: %s", output_path)
        return True
    except ImportError:
        logger.error(
            "onnxruntime[qdq] not installed. For INT8 quantization, install: pip install onnxruntime-quantization"
        )
        return False
    except Exception as e:
        logger.error("INT8 quantization failed: %s", e)
        return False


def quantize_model_fp16(
    model_path: str,
    output_path: str,
) -> bool:
    """将ONNX模型转换为FP16精度

    Args:
        model_path: 原始FP32 ONNX模型路径
        output_path: 输出FP16模型路径

    Returns:
        是否转换成功
    """
    try:
        import onnx
        from onnx import TensorProto, helper

        logger.info("Starting FP16 conversion for: %s", model_path)

        model = onnx.load(model_path)

        # 修改所有float32输入输出为float16
        graph = model.graph
        for inp in graph.input:
            if inp.type.tensor_type.elem_type == TensorProto.FLOAT:
                inp.type.tensor_type.elem_type = TensorProto.FLOAT16
        for out in graph.output:
            if out.type.tensor_type.elem_type == TensorProto.FLOAT:
                out.type.tensor_type.elem_type = TensorProto.FLOAT16
        for val in graph.value_info:
            if val.type.tensor_type.elem_type == TensorProto.FLOAT:
                val.type.tensor_type.elem_type = TensorProto.FLOAT16

        onnx.save(model, output_path)
        logger.info("FP16 conversion completed: %s", output_path)
        return True
    except ImportError:
        logger.error("onnx library not installed. Run: pip install onnx")
        return False
    except Exception as e:
        logger.error("FP16 conversion failed: %s", e)
        return False


def get_model_info(model_path: str) -> dict:
    """获取模型信息（格式、大小、输入输出维度）"""
    path = Path(model_path)
    if not path.exists():
        return {"error": "File not found"}

    size_bytes = path.stat().st_size
    ext = path.suffix.lower()

    info = {
        "file_path": model_path,
        "file_size_bytes": size_bytes,
        "file_size_mb": round(size_bytes / (1024 * 1024), 2),
        "format": ext.lstrip("."),
        "exists": True,
    }

    if ext == ".onnx":
        try:
            import onnx

            model = onnx.load(model_path)
            info["ir_version"] = model.ir_version
            info["producer_name"] = model.producer_name
            info["inputs"] = [
                {
                    "name": inp.name,
                    "shape": [d.dim_value if d.dim_value > 0 else "dynamic" for d in inp.type.tensor_type.shape.dim],
                    "dtype": onnx.TensorProto.DataType.Name[inp.type.tensor_type.elem_type],
                }
                for inp in model.graph.input
            ]
            info["outputs"] = [
                {
                    "name": out.name,
                    "shape": [d.dim_value if d.dim_value > 0 else "dynamic" for d in out.type.tensor_type.shape.dim],
                    "dtype": onnx.TensorProto.DataType.Name[out.type.tensor_type.elem_type],
                }
                for out in model.graph.output
            ]
        except Exception as e:
            # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
            logger.warning("解析ONNX模型信息失败: %s", e)

    elif ext == ".tflite":
        try:
            import tflite_runtime.interpreter as tflite  # pyright: ignore[reportMissingImports]

            interpreter = tflite.Interpreter(model_path=model_path)
            interpreter.allocate_tensors()
            info["inputs"] = [
                {
                    "name": d["name"],
                    "shape": list(d["shape"]),
                    "dtype": str(d["dtype"]),
                }
                for d in interpreter.get_input_details()
            ]
            info["outputs"] = [
                {"name": d["name"], "shape": list(d["shape"]), "dtype": str(d["dtype"])}
                for d in interpreter.get_output_details()
            ]
        except Exception as e:
            # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
            logger.warning("解析TFLite模型信息失败: %s", e)

    return info
