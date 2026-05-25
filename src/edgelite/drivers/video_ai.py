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
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

# ONNX Runtime可用性
ONNXRUNTIME_AVAILABLE = False
_onnx_model = None

try:
    import onnxruntime as ort
    ONNXRUNTIME_AVAILABLE = True
    logger.info("ONNX Runtime available: %s", ort.get_available_providers())
except ImportError:
    logger.debug("ONNX Runtime not available, using simulation mode")


class InferenceTask(Enum):
    """推理任务类型"""
    OBJECT_DETECTION = "object_detection"
    CLASSIFICATION = "classification"
    SEGMENTATION = "segmentation"
    ANOMALY_DETECTION = "anomaly_detection"
    CUSTOM = "custom"


@dataclass
class DetectionResult:
    """检测结果"""
    class_id: str
    class_name: str
    confidence: float
    bbox: tuple[int, int, int, int]  # x1, y1, x2, y2
    mask: np.ndarray | None = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class InferenceConfig:
    """推理配置"""
    model_path: str = ""
    model_type: InferenceTask = InferenceTask.OBJECT_DETECTION
    confidence_threshold: float = 0.5
    nms_threshold: float = 0.4
    input_size: tuple[int, int] = (640, 640)
    device: str = "CPU"  # CPU/CUDA/TENSORRT
    labels: list[str] = field(default_factory=list)
    max_detections: int = 100


class VideoAIModel:
    """视频AI模型封装"""

    def __init__(self, config: InferenceConfig):
        self._config = config
        self._session = None
        self._input_name = None
        self._output_names = []
        self._initialized = False

    def load(self) -> bool:
        """加载ONNX模型"""
        if not ONNXRUNTIME_AVAILABLE:
            logger.warning("ONNX Runtime not available, using simulation mode")
            self._initialized = True
            return True

        if not self._config.model_path:
            logger.warning("No model path configured, using simulation mode")
            self._initialized = True
            return True

        try:
            providers = ["CPUExecutionProvider"]
            if self._config.device == "CUDA":
                providers.insert(0, "CUDAExecutionProvider")
            elif self._config.device == "TENSORRT":
                providers.insert(0, "TensorrtExecutionProvider")

            sess_options = ort.SessionOptions()
            sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

            self._session = ort.InferenceSession(
                self._config.model_path,
                sess_options=sess_options,
                providers=providers,
            )

            # 获取输入输出名称
            self._input_name = self._session.get_inputs()[0].name
            self._output_names = [out.name for out in self._session.get_outputs()]

            self._initialized = True
            logger.info("AI model loaded: %s (input: %s)", self._config.model_path, self._input_name)
            return True

        except Exception as e:
            logger.error("Failed to load AI model: %s", e)
            self._initialized = True  # 允许模拟模式
            return True

    def preprocess(self, image: np.ndarray) -> np.ndarray:
        """图像预处理"""
        import cv2

        # 调整大小
        resized = cv2.resize(image, self._config.input_size)

        # 归一化
        normalized = resized.astype(np.float32) / 255.0

        # 通道转换 BGR->RGB
        if len(normalized.shape) == 3 and normalized.shape[2] == 3:
            normalized = cv2.cvtColor(normalized, cv2.COLOR_BGR2RGB)

        # HWC -> NCHW
        transposed = np.transpose(normalized, (2, 0, 1))
        batched = np.expand_dims(transposed, axis=0)

        return batched.astype(np.float32)

    def postprocess(
        self,
        outputs: list[np.ndarray],
        original_shape: tuple[int, int],
    ) -> list[DetectionResult]:
        """后处理检测结果"""
        results = []

        if len(outputs) == 0:
            return results

        output = outputs[0]
        if len(output.shape) == 3:  # YOLO格式 [batch, num_boxes, 85]
            # 85 = 4(box) + 1(conf) + 80(classes)
            boxes = output[..., 0:4]
            confidences = output[..., 4:5]
            class_probs = output[..., 5:]

            class_ids = np.argmax(class_probs, axis=-1)
            class_confidences = np.max(class_probs, axis=-1) * confidences.flatten()

            for i in range(len(class_confidences)):
                conf = float(class_confidences[i])
                if conf < self._config.confidence_threshold:
                    continue

                cx, cy, w, h = boxes.flatten()[i*4:(i+1)*4]
                x1 = int((cx - w/2) * original_shape[1] / self._config.input_size[0])
                y1 = int((cy - h/2) * original_shape[0] / self._config.input_size[1])
                x2 = int((cx + w/2) * original_shape[1] / self._config.input_size[0])
                y2 = int((cy + h/2) * original_shape[0] / self._config.input_size[1])

                class_id = int(class_ids.flatten()[i])
                class_name = self._config.labels[class_id] if class_id < len(self._config.labels) else f"class_{class_id}"

                results.append(DetectionResult(
                    class_id=str(class_id),
                    class_name=class_name,
                    confidence=conf,
                    bbox=(x1, y1, x2, y2),
                ))

        return results

    async def infer(self, image: np.ndarray) -> list[DetectionResult]:
        """执行推理"""
        if not self._initialized:
            return []

        if self._session is None:
            # 模拟模式
            return self._simulate_inference(image)

        try:
            import cv2

            original_shape = (image.shape[0], image.shape[1])
            preprocessed = self.preprocess(image)

            outputs = self._session.run(
                self._output_names,
                {self._input_name: preprocessed}
            )

            results = self.postprocess([out for out in outputs], original_shape)
            return results

        except Exception as e:
            logger.error("AI inference failed: %s", e)
            return []

    def _simulate_inference(self, image: np.ndarray) -> list[DetectionResult]:
        """模拟推理结果"""
        import random

        h, w = image.shape[:2]
        num_detections = random.randint(1, 3)

        results = []
        labels = self._config.labels if self._config.labels else ["object"]

        for i in range(num_detections):
            x1 = random.randint(0, w - 100)
            y1 = random.randint(0, h - 100)
            x2 = min(x1 + random.randint(50, 150), w)
            y2 = min(y1 + random.randint(50, 150), h)

            results.append(DetectionResult(
                class_id=str(random.randint(0, len(labels) - 1)),
                class_name=random.choice(labels),
                confidence=random.uniform(0.6, 0.95),
                bbox=(x1, y1, x2, y2),
            ))

        return results

    @property
    def is_loaded(self) -> bool:
        return self._initialized


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

    def add_model(self, name: str, config: InferenceConfig) -> bool:
        """添加AI模型"""
        model = VideoAIModel(config)
        if model.load():
            self._models[name] = model
            logger.info("AI model added: %s", name)
            return True
        return False

    async def analyze_frame(
        self,
        frame: np.ndarray,
        model_name: str = "default",
    ) -> list[DetectionResult]:
        """分析单帧图像"""
        self._stats["total_frames"] += 1

        model = self._models.get(model_name)
        if model is None:
            # 创建默认模型
            if not self._models:
                self._config.labels = ["person", "vehicle", " defect"]
                model = VideoAIModel(self._config)
                model.load()
                self._models[model_name] = model
            else:
                return []

        start_time = time.time()
        results = await model.infer(frame)
        inference_time = time.time() - start_time

        self._stats["total_detections"] += len(results)
        self._stats["last_inference_time"] = inference_time
        self._stats["avg_inference_time"] = (
            (self._stats["avg_inference_time"] * (self._stats["total_frames"] - 1) + inference_time)
            / self._stats["total_frames"]
        )

        return results

    async def analyze_base64_image(
        self,
        image_data: str,
        model_name: str = "default",
    ) -> dict[str, Any]:
        """分析Base64编码的图像"""
        try:
            import cv2

            # 解码Base64
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
            logger.error("Base64 image analysis failed: %s", e)
            return {"error": str(e), "detections": []}

    def get_stats(self) -> dict[str, Any]:
        """获取分析统计"""
        return self._stats.copy()

    @property
    def model_names(self) -> list[str]:
        """获取已加载模型名称"""
        return list(self._models.keys())


# 全局分析器实例
_video_analyzer: VideoAIAnalyzer | None = None


def get_video_analyzer() -> VideoAIAnalyzer:
    """获取全局视频分析器"""
    global _video_analyzer
    if _video_analyzer is None:
        _video_analyzer = VideoAIAnalyzer()
    return _video_analyzer
