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

import numpy as np

try:
    import onnxruntime as ort

    _HAS_ONNX = True
except ImportError:
    _HAS_ONNX = False
    ort = None

from edgelite.models.ai_model import (
    AiInferenceLogORM,
    AiModelORM,
    ModelStatus,
    ModelType,
)

logger = logging.getLogger(__name__)


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
        if not _HAS_ONNX:
            self.status = "unavailable"
            logger.warning("ONNX Runtime not installed, model %s marked as unavailable", self.model_id)  # FIXED-P3: 中文日志→英文
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
        self._recent_latencies: list[int] = []
        self._max_recent: int = 100

    def record_inference(self, model_id: str, latency_ms: int, status: str) -> None:
        self._total_calls += 1
        self._total_latency_ms += latency_ms
        self._per_model_calls[model_id] = self._per_model_calls.get(model_id, 0) + 1
        self._per_model_latency[model_id] = self._per_model_latency.get(model_id, 0) + latency_ms
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
            "max_latency_ms": 0,
            "min_latency_ms": 0,
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

    async def initialize(self, event_bus: Any = None, db_session_factory: Any = None) -> None:
        if not self._enabled:
            logger.info("AI inference engine disabled")  # FIXED-P3: 中文日志→英文
            return
        self._event_bus = event_bus
        self._db_session_factory = db_session_factory
        if not _HAS_ONNX:
            logger.warning("onnxruntime not installed, AI inference engine unavailable, run: pip install onnxruntime")  # FIXED-P3: 中文日志→英文
        self._models_dir.mkdir(parents=True, exist_ok=True)
        await self.load_preset_models()
        logger.info("AI inference engine initialized, %d models loaded", len(self._loaded_models))  # FIXED-P3: 中文日志→英文

    async def load_preset_models(self) -> None:
        for preset in PRESET_MODELS:
            model_path = str(self._models_dir / preset["model_file"])
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
            if Path(model_path).exists():
                await wrapper.load()
            else:
                wrapper.status = "unavailable"
                logger.warning("Preset model file not found: %s", model_path)  # FIXED-P3: 中文日志→英文
            self._loaded_models[preset["model_id"]] = wrapper

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
            input_name = wrapper.session.get_inputs()[0].name
            shape = wrapper.input_schema.get("shape", [1, -1])
            arr = np.array(input_data, dtype=np.float32).reshape(shape)
            loop = asyncio.get_running_loop()
            raw_output = await loop.run_in_executor(
                None,
                lambda: wrapper.session.run(None, {input_name: arr}),
            )
            latency_ms = int((time.perf_counter() - start) * 1000)
            output_data = {}
            for i, out in enumerate(raw_output):
                output_data[f"output_{i}"] = out.tolist() if isinstance(out, np.ndarray) else out
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
            return False, "模型不存在"
        if wrapper.status == "unavailable":
            model_path = Path(wrapper.model_path)
            if not model_path.exists():
                return False, "请先部署模型文件到 models/ 目录，当前缺失: " + wrapper.model_path
            return False, "模型文件不存在或无法加载，无法启用"
        if wrapper.status == "error":
            return False, "模型加载曾失败，请尝试热加载代替"
        if wrapper.status == "inactive":
            await wrapper.load()
            if wrapper.status != "active":
                return False, f"模型启用失败，当前状态: {wrapper.status}"
        return True, ""

    async def disable_model(self, model_id: str) -> None:
        wrapper = self._loaded_models.get(model_id)
        if wrapper:
            await wrapper.unload()

    async def remove_model(self, model_id: str) -> None:
        wrapper = self._loaded_models.pop(model_id, None)
        if wrapper:
            await wrapper.unload()

    async def shutdown(self) -> None:
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
