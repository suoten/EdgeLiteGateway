"""边缘AI推理引擎 - ONNX Runtime封装"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
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

    对于1D输入/1D输出: 生成 Identity 模型
    对于多维度: 生成 y = 0.01 * W @ x + b 线性模型
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
        # MatMul([1, in_dim], [in_dim, out_dim]) -> [1, out_dim]
        # 输出shape必须与实际计算结果一致，否则onnxruntime会报警告
        actual_output_shape = [input_shape[0], out_dim] if len(input_shape) >= 2 else output_shape
        Y = helper.make_tensor_value_info("output", TensorProto.FLOAT, actual_output_shape)
        matmul_node = helper.make_node("MatMul", inputs=["input", "W"], outputs=["matmul_out"])
        add_node = helper.make_node("Add", inputs=["matmul_out", "b"], outputs=["output"])
        graph = helper.make_graph(
            [matmul_node, add_node], "preset_graph", [X], [Y], initializer=[W_init, b_init]
        )

    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 7
    model.model_version = 1
    model.doc_string = model_file
    try:
        onnx.checker.check_model(model)
    except Exception:
        pass
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
    ):
        self.model_id = model_id
        self.model_name = model_name
        self.model_version = model_version
        self.model_type = model_type
        self.is_preset = is_preset
        self.model_path = model_path
        self.input_schema = input_schema
        self.output_schema = output_schema
        self.status: Literal["active", "inactive", "loading", "error", "unavailable"] = "inactive"
        self.session: Any = None
        self.loaded_at: datetime | None = None

    async def load(self) -> None:
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
            sess_opts = ort.SessionOptions()
            sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            sess_opts.intra_op_num_threads = 1
            sess_opts.inter_op_num_threads = 1
            loop = asyncio.get_running_loop()
            self.session = await loop.run_in_executor(
                None,
                lambda: ort.InferenceSession(self.model_path, sess_opts=sess_opts),
            )
            self.status = "active"
            self.loaded_at = datetime.now(UTC)
            logger.info("Model loaded successfully: %s (%s)", self.model_id, self.model_name)  # FIXED-P3: 中文日志→英文
        except Exception as e:
            self.status = "error"
            self.session = None
            logger.error("Model load failed: %s - %s", self.model_id, e)  # FIXED-P3: 中文日志→英文

    async def unload(self) -> None:
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
        self._total_calls += 1
        self._total_latency_ms += latency_ms
        self._per_model_calls[model_id] = self._per_model_calls.get(model_id, 0) + 1
        self._per_model_latency[model_id] = self._per_model_latency.get(model_id, 0) + latency_ms
        # Update max latency
        current_max = self._per_model_max_latency.get(model_id, 0)
        if latency_ms > current_max:
            self._per_model_max_latency[model_id] = latency_ms
        # Update min latency
        current_min = self._per_model_min_latency.get(model_id)
        if current_min is None or latency_ms < current_min:
            self._per_model_min_latency[model_id] = latency_ms
        self._recent_latencies.append(latency_ms)
        if len(self._recent_latencies) > self._max_recent:
            self._recent_latencies = self._recent_latencies[-self._max_recent:]
        if status == "error":
            self._total_errors += 1
            self._per_model_errors[model_id] = self._per_model_errors.get(model_id, 0) + 1

    def get_snapshot(self) -> dict:
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
    {
        "model_id": "preset-vibration-v1",
        "model_name": "Vibration Analysis v1",
        "model_version": "v1.0.0",
        "model_type": ModelType.ANOMALY,
        "model_file": "elg-vibration-v1.onnx",
        "input_schema": {"shape": [1, 128], "dtype": "float32", "description": "最近128个振动数据点"},
        "output_schema": {"shape": [1, 2], "dtype": "float32", "description": "振动模式分类和异常分数"},
    },
    {
        "model_id": "preset-power-v1",
        "model_name": "Power Consumption Prediction v1",
        "model_version": "v1.0.0",
        "model_type": ModelType.TREND,
        "model_file": "elg-power-v1.onnx",
        "input_schema": {"shape": [1, 168], "dtype": "float32", "description": "最近168小时能耗数据"},
        "output_schema": {"shape": [1, 24], "dtype": "float32", "description": "未来24小时能耗预测"},
    },
    {
        "model_id": "preset-quality-v1",
        "model_name": "Quality Inspection v1",
        "model_version": "v1.0.0",
        "model_type": ModelType.THRESHOLD,
        "model_file": "elg-quality-v1.onnx",
        "input_schema": {"shape": [1, 50], "dtype": "float32", "description": "最近50个工艺参数"},
        "output_schema": {"shape": [1], "dtype": "float32", "description": "质量评分0-100"},
    },
    {
        "model_id": "preset-battery-v1",
        "model_name": "Battery Health v1",
        "model_version": "v1.0.0",
        "model_type": ModelType.CUSTOM,
        "model_file": "elg-battery-v1.onnx",
        "input_schema": {"shape": [1, 100], "dtype": "float32", "description": "最近100个充放电循环数据"},
        "output_schema": {"shape": [1], "dtype": "float32", "description": "SOH健康度百分比"},
    },
    {
        "model_id": "preset-leak-v1",
        "model_name": "Leak Detection v1",
        "model_version": "v1.0.0",
        "model_type": ModelType.ANOMALY,
        "model_file": "elg-leak-v1.onnx",
        "input_schema": {"shape": [1, 60], "dtype": "float32", "description": "最近60个压力/流量数据点"},
        "output_schema": {"shape": [1], "dtype": "float32", "description": "泄漏概率0-1"},
    },
]


class AiInferenceEngine:
    """边缘AI推理引擎"""

    def __init__(self, models_dir: str, enabled: bool = True):
        self._models_dir = Path(models_dir)
        self._enabled = enabled
        self._loaded_models: dict[str, OnnxModelWrapper] = {}
        self._stats = InferenceStatsCollector()
        self._event_bus: Any = None
        self._lock = asyncio.Lock()
        self._db_session_factory: Any = None
        self._scheduled_tasks: dict[str, asyncio.Task] = {}
        self._scheduled_configs: dict[str, dict] = {}

    async def initialize(self, event_bus: Any = None, db_session_factory: Any = None) -> None:
        if not self._enabled:
            logger.info("AI inference engine disabled")
            return
        self._event_bus = event_bus
        self._db_session_factory = db_session_factory
        if not _check_onnxruntime():
            logger.warning(
                "onnxruntime not installed, AI models will be inactive. "
                "Install onnxruntime and enable models via API: pip install onnxruntime"
            )
        self._models_dir.mkdir(parents=True, exist_ok=True)
        await self.load_preset_models()
        logger.info("AI inference engine initialized, %d models loaded", len(self._loaded_models))

    async def load_preset_models(self) -> None:
        for preset in PRESET_MODELS:
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
                    model_path, abs_model_path,
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
            target_path.write_bytes(model_bytes)
            logger.info("Auto-generated preset model: %s (%d bytes)", preset["model_file"], len(model_bytes))
            return True
        except Exception as e:
            logger.warning("Failed to auto-generate preset model %s: %s", preset["model_file"], e)
            return False

    async def reload_model(self, model_id: str, model_path: str) -> None:
        async with self._lock:
            wrapper = self._loaded_models.get(model_id)
            if wrapper is None:
                raise ValueError(f"Model not found: {model_id}")
            old_status = wrapper.status
            wrapper.model_path = model_path
            await wrapper.unload()
            await wrapper.load()
            logger.info("Model hot-reload completed: %s (%s -> %s)", model_id, old_status, wrapper.status)  # FIXED-P3: 中文日志→英文

    async def load_custom_model(
        self,
        model_id: str,
        model_name: str,
        model_version: str,
        model_type: str,
        model_path: str,
        input_schema: dict,
        output_schema: dict,
    ) -> OnnxModelWrapper:
        async with self._lock:
            wrapper = OnnxModelWrapper(
                model_id=model_id,
                model_name=model_name,
                model_version=model_version,
                model_type=model_type,
                is_preset=False,
                model_path=model_path,
                input_schema=input_schema,
                output_schema=output_schema,
            )
            await wrapper.load()
            self._loaded_models[model_id] = wrapper
            return wrapper

    async def infer(self, model_id: str, input_data: list[float]) -> InferenceResult:
        wrapper = self._loaded_models.get(model_id)
        if wrapper is None or wrapper.status != "active":
            return InferenceResult(
                model_id=model_id,
                output_data={},
                latency_ms=0,
                status="error",
                error_message=f"Model not available: {model_id}",
            )
        start = time.perf_counter()
        try:
            if not _HAS_NUMPY:
                return InferenceResult(
                    model_id=model_id,
                    output_data={},
                    latency_ms=0,
                    status="error",
                    error_message="numpy not installed, required for inference. Run: pip install numpy",
                )
            input_name = wrapper.session.get_inputs()[0].name
            shape = wrapper.input_schema.get("shape", [1, -1])
            expected_size = 1
            for dim in shape:
                if dim > 0:
                    expected_size *= dim
            if len(input_data) != expected_size:
                return InferenceResult(
                    model_id=model_id,
                    output_data={},
                    latency_ms=0,
                    status="error",
                    error_message=f"Input size mismatch: expected {expected_size} (shape {shape}), got {len(input_data)}",
                )
            arr = np.array(input_data, dtype=np.float32).reshape(shape)
            loop = asyncio.get_running_loop()
            raw_output = await loop.run_in_executor(
                None,
                lambda: wrapper.session.run(None, {input_name: arr}),
            )
            latency_ms = int((time.perf_counter() - start) * 1000)
            output_data = {}
            for i, out in enumerate(raw_output):
                if isinstance(out, np.ndarray):
                    val = out.tolist()
                    # 将嵌套列表展平：[[0.5]] -> [0.5], [[1,2,3]] -> [1,2,3]
                    if isinstance(val, list) and len(val) == 1 and isinstance(val[0], list):
                        val = val[0]
                    output_data[f"output_{i}"] = val
                else:
                    output_data[f"output_{i}"] = out
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
            return result
        except Exception as e:
            latency_ms = int((time.perf_counter() - start) * 1000)
            self._stats.record_inference(model_id, latency_ms, "error")
            return InferenceResult(
                model_id=model_id,
                output_data={},
                latency_ms=latency_ms,
                status="error",
                error_message=str(e),
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
        wrapper = self._loaded_models.get(model_id)
        if wrapper:
            await wrapper.unload()

    async def remove_model(self, model_id: str) -> None:
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
                model_id, device_id, point_name, interval_seconds, input_window_size,
            ),
            name=f"scheduled_inference_{model_id}",
        )
        self._scheduled_tasks[model_id] = task
        logger.info(
            "Scheduled inference started: model=%s, device=%s, point=%s, interval=%ds",
            model_id, device_id, point_name, interval_seconds,
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
                        device_id, point_name, input_window_size,
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
                            device_id, point_name,
                        )
                except Exception as e:
                    logger.error(
                        "Scheduled inference error: model=%s - %s", model_id, e,
                    )
                await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            logger.info("Scheduled inference cancelled: model=%s", model_id)
        finally:
            self._scheduled_tasks.pop(model_id, None)

    async def _fetch_influx_data(
        self, device_id: str, point_name: str, window_size: int,
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

            values = [
                d["value"] for d in data
                if d.get("value") is not None and isinstance(d["value"], (int, float))
            ]
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
        task = self._scheduled_tasks.pop(model_id, None)
        self._scheduled_configs.pop(model_id, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            logger.info("Scheduled inference stopped: model=%s", model_id)
            return True
        return False

    def get_scheduled_inferences(self) -> list[dict]:
        """Get all scheduled inference configurations"""
        result = []
        for model_id, config in self._scheduled_configs.items():
            task = self._scheduled_tasks.get(model_id)
            entry = dict(config)
            entry["running"] = task is not None and not task.done()
            result.append(entry)
        return result

    async def shutdown(self) -> None:
        # Cancel all scheduled inference tasks
        for model_id in list(self._scheduled_tasks.keys()):
            await self.stop_scheduled_inference(model_id)
        for wrapper in self._loaded_models.values():
            await wrapper.unload()
        self._loaded_models.clear()
        logger.info("AI inference engine shutdown, all model resources released")  # FIXED-P3: 中文日志→英文


class AiInferenceEvent:
    """AI推理结果事件"""

    __slots__ = ("model_id", "output_data", "latency_ms", "timestamp")

    def __init__(self, model_id: str, output_data: dict, latency_ms: int):
        self.model_id = model_id
        self.output_data = output_data
        self.latency_ms = latency_ms
        self.timestamp = datetime.now(UTC)
