"""视频AI分析模块 - 基于OpenCV和ONNX Runtime的边缘AI推理

本模块提供工业场景视频AI分析能力:
- 物体检测 (YOLO/SSD等)
- 缺陷检测 (自定义模型)
- 行为分析 (安全帽/入侵检测)
- 计数统计 (产品计数/人员统计)

支持ONNX Runtime加速推理，适配多种AI模型。
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

ONNXRUNTIME_AVAILABLE = False
_onnx_model = None

try:
    import onnxruntime as ort

    ONNXRUNTIME_AVAILABLE = True
    logger.info("ONNX Runtime available: %s", ort.get_available_providers())
except ImportError:
    logger.debug("ONNX Runtime not available, using simulation mode")

_ALLOWED_INPUT_DTYPES = {"tensor(float)", "tensor(uint8)", "tensor(int64)", "tensor(int32)"}
_ONNX_MAGIC_BYTES = b"\x08\x07"
_DEFAULT_MODEL_DIRS: list[str] = []


def validate_model_path(
    model_path: str,
    allowed_dirs: list[str] | None = None,
) -> ModelValidateResult:
    if not model_path:
        return ModelValidateResult(ok=False, error_code="ERR_VAI_MODEL_VALIDATE_FAILED", detail="model_path is empty")

    norm_path = os.path.normpath(os.path.abspath(model_path))

    if ".." in model_path or ".." in norm_path:
        return ModelValidateResult(
            ok=False, error_code="ERR_VAI_MODEL_PATH_TRAVERSAL", detail=f"Path traversal detected: {model_path}"
        )

    dirs = allowed_dirs if allowed_dirs else _DEFAULT_MODEL_DIRS
    if dirs:
        in_allowed = False
        for d in dirs:
            norm_dir = os.path.normpath(os.path.abspath(d))
            if norm_path.startswith(norm_dir + os.sep) or norm_path == norm_dir:
                in_allowed = True
                break
        if not in_allowed:
            return ModelValidateResult(
                ok=False,
                error_code="ERR_VAI_MODEL_PATH_NOT_ALLOWED",
                detail=f"Path not in allowed directories: {model_path}",
            )

    if not model_path.lower().endswith(".onnx"):
        return ModelValidateResult(
            ok=False, error_code="ERR_VAI_MODEL_FORMAT_INVALID", detail=f"File extension not .onnx: {model_path}"
        )

    if os.path.isfile(norm_path):
        try:
            with open(norm_path, "rb") as f:
                header = f.read(2)
                if len(header) >= 2 and header[:2] == _ONNX_MAGIC_BYTES:
                    pass
                elif len(header) >= 2:
                    return ModelValidateResult(
                        ok=False,
                        error_code="ERR_VAI_MODEL_HEADER_INVALID",
                        detail=f"File header does not match ONNX protobuf magic: {header[:2].hex()}",
                    )
        except OSError as e:
            return ModelValidateResult(
                ok=False, error_code="ERR_VAI_MODEL_VALIDATE_FAILED", detail=f"Cannot read model file: {e}"
            )

    return ModelValidateResult(ok=True)


class InferenceTask(Enum):
    OBJECT_DETECTION = "object_detection"
    CLASSIFICATION = "classification"
    SEGMENTATION = "segmentation"
    ANOMALY_DETECTION = "anomaly_detection"
    CUSTOM = "custom"


@dataclass
class DetectionResult:
    class_id: str
    class_name: str
    confidence: float
    bbox: tuple[int, int, int, int]
    mask: np.ndarray | None = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class InferenceConfig:
    model_path: str = ""
    model_type: InferenceTask = InferenceTask.OBJECT_DETECTION
    confidence_threshold: float = 0.5
    nms_threshold: float = 0.4
    input_size: tuple[int, int] = (640, 640)
    device: str = "CPU"
    labels: list[str] = field(default_factory=list)
    max_detections: int = 100
    inference_timeout: float = 5.0


@dataclass
class InferenceOutput:
    boxes: list[dict[str, Any]] = field(default_factory=list)
    image_shape: list[int] = field(default_factory=lambda: [0, 0, 0])
    object_count: int = 0
    confidence_avg: float = 0.0
    inference_time_ms: float = 0.0
    error_code: str = ""
    error_detail: str = ""

    @property
    def ok(self) -> bool:
        return not self.error_code


@dataclass
class ModelValidateResult:
    ok: bool
    error_code: str = ""
    detail: str = ""

    def __bool__(self) -> bool:
        return self.ok


def validate_onnx_model(
    model_path: str,
    expected_input_size: tuple[int, int] | None = None,
    expected_input_dtype: str | None = None,
) -> ModelValidateResult:
    if not ONNXRUNTIME_AVAILABLE:
        return ModelValidateResult(
            ok=False, error_code="ERR_VAI_MODEL_VALIDATE_FAILED", detail="ONNX Runtime not available"
        )

    if not model_path or not os.path.isfile(model_path):
        return ModelValidateResult(
            ok=False, error_code="ERR_VAI_MODEL_VALIDATE_FAILED", detail=f"Model file not found: {model_path}"
        )

    try:
        import onnx

        onnx_model = onnx.load(model_path)
        onnx.checker.check_model(onnx_model, full_check=False)
    except ImportError:
        pass
    except Exception as e:
        return ModelValidateResult(
            ok=False, error_code="ERR_VAI_MODEL_VALIDATE_FAILED", detail=f"Invalid ONNX format: {e}"
        )

    try:
        sess_options = ort.SessionOptions()
        sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_DISABLE_ALL
        session = ort.InferenceSession(model_path, sess_options=sess_options, providers=["CPUExecutionProvider"])

        inputs = session.get_inputs()
        if not inputs:
            return ModelValidateResult(
                ok=False, error_code="ERR_VAI_MODEL_VALIDATE_INPUT_MISMATCH", detail="Model has no inputs"
            )

        inp = inputs[0]
        shape = inp.shape
        if len(shape) == 4 and expected_input_size:
            in_h = shape[2] if isinstance(shape[2], int) else None
            in_w = shape[3] if isinstance(shape[3], int) else None
            if in_h is not None and in_w is not None and (in_h, in_w) != expected_input_size:
                return ModelValidateResult(
                    ok=False,
                    error_code="ERR_VAI_MODEL_VALIDATE_INPUT_MISMATCH",
                    detail=f"Input size ({in_w},{in_h}) != expected {expected_input_size}",
                )

        if expected_input_dtype:
            actual_dtype = inp.type
            if actual_dtype != expected_input_dtype and actual_dtype not in _ALLOWED_INPUT_DTYPES:
                return ModelValidateResult(
                    ok=False,
                    error_code="ERR_VAI_MODEL_VALIDATE_DTYPE_MISMATCH",
                    detail=f"Input dtype {actual_dtype} != expected {expected_input_dtype}",
                )

        outputs = session.get_outputs()
        if not outputs:
            return ModelValidateResult(
                ok=False, error_code="ERR_VAI_MODEL_VALIDATE_OUTPUT_MISMATCH", detail="Model has no outputs"
            )

        del session
        return ModelValidateResult(ok=True)

    except Exception as e:
        logger.error("Model validation failed: %s", e)
        return ModelValidateResult(
            ok=False, error_code="ERR_VAI_MODEL_VALIDATE_FAILED", detail=type(e).__name__
        )  # FIXED-P2: 仅返回异常类型名，防止泄露模型路径/内部详情


def resolve_device(requested_device: str) -> tuple[str, bool]:
    if not ONNXRUNTIME_AVAILABLE:
        return "CPU", False

    available = ort.get_available_providers()
    if requested_device == "CUDA":
        if "CUDAExecutionProvider" in available:
            return "CUDA", False
        logger.warning("[video_ai] CUDA not available (providers: %s), degrading to CPU", available)
        return "CPU", True
    if requested_device == "TENSORRT":
        if "TensorrtExecutionProvider" in available:
            return "TENSORRT", False
        logger.warning("[video_ai] TensorRT not available (providers: %s), degrading to CPU", available)
        return "CPU", True
    return "CPU", False


class VideoAIModel:
    """视频AI模型封装"""

    def __init__(self, config: InferenceConfig):
        self._config = config
        self._session = None
        self._input_name = None
        self._output_names = []
        self._initialized = False
        self._actual_device: str = "CPU"
        self._gpu_degraded: bool = False

    def load(self) -> bool:
        # FIXED(一般): 原问题-重复load()泄漏旧ONNX session;
        # 修复-load前先unload旧session
        if self._session is not None:
            try:
                self.unload()
            except Exception as unload_err:
                logger.warning("[video_ai] Unload old session before reload failed: %s", unload_err, exc_info=True)

        if not ONNXRUNTIME_AVAILABLE:
            logger.warning("[video_ai] ONNX Runtime not available, using simulation mode")
            self._initialized = True
            return True

        if not self._config.model_path:
            logger.warning("[video_ai] No model path configured, using simulation mode")
            self._initialized = True
            return True

        actual_device, degraded = resolve_device(self._config.device)
        self._actual_device = actual_device
        self._gpu_degraded = degraded

        try:
            providers = ["CPUExecutionProvider"]
            if actual_device == "CUDA":
                providers.insert(0, "CUDAExecutionProvider")
            elif actual_device == "TENSORRT":
                providers.insert(0, "TensorrtExecutionProvider")

            sess_options = ort.SessionOptions()
            sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

            self._session = ort.InferenceSession(
                self._config.model_path,
                sess_options=sess_options,
                providers=providers,
            )

            self._input_name = self._session.get_inputs()[0].name
            self._output_names = [out.name for out in self._session.get_outputs()]

            self._initialized = True
            logger.info(
                "AI model loaded: %s (input: %s, device: %s)", self._config.model_path, self._input_name, actual_device
            )
            return True

        except Exception as e:
            logger.error("[video_ai] Failed to load AI model: %s", e)
            self._session = None
            self._initialized = False
            return False

    def unload(self) -> None:
        if self._session is not None:
            self._session = None
        self._input_name = None
        self._output_names = []
        self._initialized = False

    def preprocess(self, image: np.ndarray) -> np.ndarray:
        try:
            import cv2

            if image is None or not isinstance(image, np.ndarray):
                raise ValueError("Input image is None or not ndarray")
            if image.size == 0:
                raise ValueError("Input image is empty")

            resized = cv2.resize(image, self._config.input_size)

            normalized = resized.astype(np.float32) / 255.0

            if len(normalized.shape) == 3 and normalized.shape[2] == 3:
                normalized = normalized[..., ::-1]

            transposed = np.transpose(normalized, (2, 0, 1))
            batched = np.expand_dims(transposed, axis=0)

            return batched.astype(np.float32)
        except Exception as e:
            raise ValueError(f"Preprocessing failed: {e}") from e

    def _nms(self, boxes: np.ndarray, scores: np.ndarray) -> np.ndarray:
        if len(boxes) == 0:
            return np.array([], dtype=np.int64)

        x1 = boxes[:, 0]
        y1 = boxes[:, 1]
        x2 = boxes[:, 2]
        y2 = boxes[:, 3]
        areas = (x2 - x1 + 1) * (y2 - y1 + 1)
        order = scores.argsort()[::-1]

        keep = []
        while order.size > 0:
            i = order[0]
            keep.append(i)
            xx1 = np.maximum(x1[i], x1[order[1:]])
            yy1 = np.maximum(y1[i], y1[order[1:]])
            xx2 = np.minimum(x2[i], x2[order[1:]])
            yy2 = np.minimum(y2[i], y2[order[1:]])
            w = np.maximum(0.0, xx2 - xx1 + 1)
            h = np.maximum(0.0, yy2 - yy1 + 1)
            inter = w * h
            denominator = areas[i] + areas[order[1:]] - inter
            denominator = np.maximum(denominator, 1e-6)  # FIXED-P2: 防止除零
            iou = inter / denominator
            inds = np.where(iou <= self._config.nms_threshold)[0]
            order = order[inds + 1]

        return np.array(keep, dtype=np.int64)

    def postprocess(
        self,
        outputs: list[np.ndarray],
        original_shape: tuple[int, int],
        confidence_threshold: float | None = None,
    ) -> list[DetectionResult]:
        results = []
        threshold = confidence_threshold if confidence_threshold is not None else self._config.confidence_threshold

        if not outputs:
            return results

        output = outputs[0]
        if len(output.shape) == 3:
            boxes = output[..., 0:4]
            confidences = output[..., 4:5]
            class_probs = output[..., 5:]

            class_ids = np.argmax(class_probs, axis=-1)
            class_confidences = np.max(class_probs, axis=-1) * confidences.flatten()

            mask = class_confidences >= threshold
            filtered_boxes = boxes[mask]
            filtered_scores = class_confidences[mask]
            filtered_class_ids = class_ids[mask]

            if len(filtered_boxes) == 0:
                return results

            orig_h, orig_w = original_shape
            in_w, in_h = self._config.input_size
            scale_x = orig_w / in_w
            scale_y = orig_h / in_h

            scaled_boxes = np.zeros_like(filtered_boxes)
            scaled_boxes[:, 0] = (filtered_boxes[:, 0] - filtered_boxes[:, 2] / 2) * scale_x
            scaled_boxes[:, 1] = (filtered_boxes[:, 1] - filtered_boxes[:, 3] / 2) * scale_y
            scaled_boxes[:, 2] = (filtered_boxes[:, 0] + filtered_boxes[:, 2] / 2) * scale_x
            scaled_boxes[:, 3] = (filtered_boxes[:, 1] + filtered_boxes[:, 3] / 2) * scale_y

            keep_indices = self._nms(scaled_boxes, filtered_scores)
            kept_boxes = scaled_boxes[keep_indices]
            kept_scores = filtered_scores[keep_indices]
            kept_class_ids = filtered_class_ids[keep_indices]

            for _i, (box, score, cid) in enumerate(zip(kept_boxes, kept_scores, kept_class_ids, strict=False)):
                x1, y1, x2, y2 = box.astype(int)
                x1 = max(0, int(x1))  # FIXED-P2: 边界框坐标裁剪到图像范围内
                y1 = max(0, int(y1))
                x2 = min(orig_w, int(x2))
                y2 = min(orig_h, int(y2))
                cid_int = int(cid)
                class_name = self._config.labels[cid_int] if cid_int < len(self._config.labels) else f"class_{cid_int}"
                results.append(
                    DetectionResult(
                        class_id=str(cid_int),
                        class_name=class_name,
                        confidence=float(score),
                        bbox=(int(x1), int(y1), int(x2), int(y2)),
                    )
                )

        return results

    async def infer(self, image: np.ndarray, confidence_threshold: float | None = None) -> list[DetectionResult]:
        if not self._initialized:
            return []

        if self._session is None:
            return self._simulate_inference(image)

        try:
            original_shape = (image.shape[0], image.shape[1])
            preprocessed = self.preprocess(image)

            def _run_inference():
                return self._session.run(self._output_names, {self._input_name: preprocessed})

            outputs = await asyncio.wait_for(
                asyncio.to_thread(_run_inference), timeout=self._config.inference_timeout
            )  # FIXED-P2: 原问题-硬编码30s超时，应使用config中的inference_timeout
            results = self.postprocess(
                [out for out in outputs], original_shape, confidence_threshold=confidence_threshold
            )
            return results

        except Exception as e:
            logger.error("[video_ai] Inference failed: %s", e)
            return []

    async def infer_structured(
        self,
        image: np.ndarray,
        confidence_threshold: float | None = None,
    ) -> InferenceOutput:
        if not self._initialized:
            return InferenceOutput(error_code="ERR_VAI_MODEL_NOT_LOADED", error_detail="Model not initialized")

        image_shape = [0, 0, 0]
        if image is not None and isinstance(image, np.ndarray) and image.size > 0:
            if len(image.shape) == 3:
                image_shape = [image.shape[0], image.shape[1], image.shape[2]]
            elif len(image.shape) == 2:
                image_shape = [image.shape[0], image.shape[1], 1]

        if image is None or not isinstance(image, np.ndarray) or image.size == 0:
            return InferenceOutput(
                image_shape=image_shape,
                error_code="ERR_VAI_PREPROCESS_FAILED",
                error_detail="Input image is None, not ndarray, or empty",
            )

        if self._session is None:
            return self._infer_simulated_structured(image, image_shape, confidence_threshold)

        start_time = time.time()

        try:
            preprocessed = self.preprocess(image)
        except Exception as e:
            logger.error("Preprocess failed: %s", e)
            return InferenceOutput(
                image_shape=image_shape,
                error_code="ERR_VAI_PREPROCESS_FAILED",
                error_detail=type(e).__name__,  # FIXED-P2: 仅返回异常类型名
            )

        try:
            original_shape = (image.shape[0], image.shape[1])

            def _run_inference():
                return self._session.run(self._output_names, {self._input_name: preprocessed})

            timeout = self._config.inference_timeout
            outputs = await asyncio.wait_for(
                asyncio.to_thread(_run_inference),
                timeout=timeout,
            )

            detections = self.postprocess(
                [out for out in outputs], original_shape, confidence_threshold=confidence_threshold
            )
            inference_time_ms = (time.time() - start_time) * 1000

            boxes = [
                {
                    "x1": d.bbox[0],
                    "y1": d.bbox[1],
                    "x2": d.bbox[2],
                    "y2": d.bbox[3],
                    "class": d.class_name,
                    "confidence": round(d.confidence, 4),
                }
                for d in detections
            ]

            object_count = len(detections)
            confidence_avg = 0.0
            if object_count > 0:
                confidence_avg = round(sum(d.confidence for d in detections) / object_count, 4)

            return InferenceOutput(
                boxes=boxes,
                image_shape=image_shape,
                object_count=object_count,
                confidence_avg=confidence_avg,
                inference_time_ms=round(inference_time_ms, 2),
            )

        except TimeoutError:
            inference_time_ms = (time.time() - start_time) * 1000
            return InferenceOutput(
                image_shape=image_shape,
                inference_time_ms=round(inference_time_ms, 2),
                error_code="ERR_VAI_INFERENCE_TIMEOUT",
                error_detail=f"Inference timed out after {self._config.inference_timeout}s",
            )
        except Exception as e:
            inference_time_ms = (time.time() - start_time) * 1000
            logger.error("Inference failed: %s", e)
            return InferenceOutput(
                image_shape=image_shape,
                inference_time_ms=round(inference_time_ms, 2),
                error_code="ERR_VAI_INFERENCE_FAILED",
                error_detail=type(e).__name__,  # FIXED-P2: 仅返回异常类型名
            )

    def _infer_simulated_structured(
        self,
        image: np.ndarray,
        image_shape: list[int],
        confidence_threshold: float | None = None,
    ) -> InferenceOutput:
        threshold = confidence_threshold if confidence_threshold is not None else self._config.confidence_threshold
        start_time = time.time()
        detections = self._simulate_inference(image)
        detections = [d for d in detections if d.confidence >= threshold]
        inference_time_ms = (time.time() - start_time) * 1000

        boxes = [
            {
                "x1": d.bbox[0],
                "y1": d.bbox[1],
                "x2": d.bbox[2],
                "y2": d.bbox[3],
                "class": d.class_name,
                "confidence": round(d.confidence, 4),
            }
            for d in detections
        ]

        object_count = len(detections)
        confidence_avg = 0.0
        if object_count > 0:
            confidence_avg = round(sum(d.confidence for d in detections) / object_count, 4)

        return InferenceOutput(
            boxes=boxes,
            image_shape=image_shape,
            object_count=object_count,
            confidence_avg=confidence_avg,
            inference_time_ms=round(inference_time_ms, 2),
        )

    def _simulate_inference(self, image: np.ndarray) -> list[DetectionResult]:
        h, w = image.shape[:2]
        rng = np.random.default_rng()
        num_detections = rng.integers(1, 4)

        results = []
        labels = self._config.labels if self._config.labels else ["object"]

        for _ in range(num_detections):
            # FIXED-P1: 原问题-rng.integers(0, max(0, w-100))当w<=100时high=0，low>=high导致numpy抛出ValueError。改为确保high>low
            x1 = rng.integers(0, max(1, w - 100))
            y1 = rng.integers(0, max(1, h - 100))
            # FIXED-P1: 确保x2>x1+50且y2>y1+50，防止low>=high
            x2_low = int(x1) + 50
            x2_high = max(x2_low + 1, min(w, int(x1) + 150))
            y2_low = int(y1) + 50
            y2_high = max(y2_low + 1, min(h, int(y1) + 150))
            x2 = rng.integers(x2_low, x2_high)
            y2 = rng.integers(y2_low, y2_high)
            x2 = min(x2, w)
            y2 = min(y2, h)

            results.append(
                DetectionResult(
                    class_id=str(rng.integers(0, max(1, len(labels)))),
                    class_name=rng.choice(labels),
                    confidence=rng.uniform(0.6, 0.95),
                    bbox=(int(x1), int(y1), int(x2), int(y2)),
                )
            )

        return results

    @property
    def is_loaded(self) -> bool:
        return self._initialized

    @property
    def actual_device(self) -> str:
        return self._actual_device

    @property
    def gpu_degraded(self) -> bool:
        return self._gpu_degraded


class VideoAIAnalyzer:
    """视频AI分析器"""

    def __init__(self):
        self._models: dict[str, VideoAIModel] = {}
        self._config = InferenceConfig()
        self._running = False
        self._stats = {
            "total_frames": 0,
            "total_detections": 0,
            "avg_inference_time": 0.0,
            "last_inference_time": 0.0,
        }
        # FIXED(严重): 原问题-_stats非原子读-改-写无锁保护; 懒加载默认模型无同步保护，并发调用可创建多个实例导致ONNX session泄漏;
        # 修复-添加asyncio.Lock保护统计更新和模型懒加载
        self._stats_lock = asyncio.Lock()
        self._model_lock = asyncio.Lock()

    def add_model(self, name: str, config: InferenceConfig) -> bool:
        model = VideoAIModel(config)
        if model.load():
            self._models[name] = model
            logger.info("AI model added: %s", name)
            return True
        return False

    async def unload_model(self, name: str) -> bool:
        # FIXED(P1): 原问题-API层直接del内部_models字典无锁保护，并发推理迭代_models时触发RuntimeError;
        # 修复-提供unload_model方法，在_model_lock内安全移除并释放ONNX session
        async with self._model_lock:
            model = self._models.pop(name, None)
        if model is not None:
            try:
                model.unload()
            except Exception as e:
                logger.warning("[video_ai] unload model %s session failed: %s", name, e)
            logger.info("AI model unloaded: %s", name)
            return True
        return False

    async def analyze_frame(
        self,
        frame: np.ndarray,
        model_name: str = "default",
    ) -> list[DetectionResult]:
        # FIXED(严重): 原问题-_stats非原子读-改-写无锁保护; 懒加载默认模型无同步保护，并发调用可创建多个实例导致ONNX session泄漏;
        # 修复-使用_stats_lock保护统计更新，使用_model_lock双重检查保护模型懒加载
        async with self._stats_lock:
            self._stats["total_frames"] += 1
            current_total = self._stats["total_frames"]

        model = self._models.get(model_name)
        if model is None:
            async with self._model_lock:
                # 双重检查：获取锁后再次确认，避免并发创建多个实例
                model = self._models.get(model_name)
                if model is None:
                    if not self._models:
                        self._config.labels = ["person", "vehicle", "defect"]  # FIXED-P2: 移除" defect"前导空格
                        model = VideoAIModel(self._config)
                        model.load()
                        self._models[model_name] = model
                    else:
                        return []

        start_time = time.time()
        results = await model.infer(frame)
        inference_time = time.time() - start_time

        async with self._stats_lock:
            self._stats["total_detections"] += len(results)
            self._stats["last_inference_time"] = inference_time
            self._stats["avg_inference_time"] = (
                self._stats["avg_inference_time"] * (current_total - 1) + inference_time
            ) / current_total

        return results

    async def analyze_base64_image(
        self,
        image_data: str,
        model_name: str = "default",
    ) -> dict[str, Any]:
        try:
            import cv2

            img_bytes = base64.b64decode(image_data)
            nparr = np.frombuffer(img_bytes, np.uint8)
            frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

            if frame is None:
                return {"error": "Failed to decode image", "detections": []}

            detections = await self.analyze_frame(frame, model_name)

            return {
                "detections": [
                    {
                        "class_id": d.class_id,
                        "class_name": d.class_name,
                        "confidence": float(d.confidence),
                        "bbox": d.bbox,
                        "timestamp": d.timestamp,
                    }
                    for d in detections
                ],
                "stats": self._stats.copy(),
            }

        except Exception as e:
            logger.error("[video_ai] Base64 image analysis failed: %s", e)
            return {"error": str(e), "detections": []}

    def get_stats(self) -> dict[str, Any]:
        return self._stats.copy()

    @property
    def model_names(self) -> list[str]:
        return list(self._models.keys())


_video_analyzer: VideoAIAnalyzer | None = None
_video_analyzer_lock = threading.Lock()  # FIXED-P1: 全局单例创建加锁保护


def get_video_analyzer() -> VideoAIAnalyzer:
    global _video_analyzer
    # FIXED-P1: 原问题-全局单例未加锁保护，并发调用可能创建多个 VideoAIAnalyzer 实例，导致模型重复加载和资源浪费
    # 修复：使用 threading.Lock 保护单例创建，采用双重检查锁定模式避免已创建后仍竞争锁
    if _video_analyzer is None:
        with _video_analyzer_lock:
            if _video_analyzer is None:
                _video_analyzer = VideoAIAnalyzer()
    return _video_analyzer
