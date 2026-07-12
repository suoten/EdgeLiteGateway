"""video_ai 模块测试 - 模型路径校验/设备解析/推理输出/模型封装

覆盖 drivers/video_ai.py 的纯函数与数据类：
- validate_model_path: 路径穿越/allowed_dirs/扩展名/ONNX magic bytes/读取异常
- resolve_device: CUDA/TENSORRT/CPU 回退降级
- ModelValidateResult / InferenceOutput / InferenceConfig / DetectionResult 数据类
- VideoAIModel: 仿真模式 load/unload/infer
"""

from __future__ import annotations

import os
from unittest.mock import patch

import numpy as np
import pytest

from edgelite.drivers import video_ai
from edgelite.drivers.video_ai import (
    DetectionResult,
    InferenceConfig,
    InferenceOutput,
    InferenceTask,
    ModelValidateResult,
    VideoAIModel,
    resolve_device,
    validate_model_path,
)


# --------------------------------------------------------------------------- #
# validate_model_path
# --------------------------------------------------------------------------- #


class TestValidateModelPath:
    """模型路径校验纯函数测试"""

    def test_empty_path_returns_validate_failed(self):
        result = validate_model_path("")
        assert result.ok is False
        assert result.error_code == "ERR_VAI_MODEL_VALIDATE_FAILED"
        assert "empty" in result.detail

    def test_none_path_returns_validate_failed(self):
        result = validate_model_path(None)  # type: ignore[arg-type]
        assert result.ok is False
        assert result.error_code == "ERR_VAI_MODEL_VALIDATE_FAILED"

    def test_path_traversal_in_raw(self):
        result = validate_model_path("../secret/model.onnx")
        assert result.ok is False
        assert result.error_code == "ERR_VAI_MODEL_PATH_TRAVERSAL"

    def test_path_traversal_in_normalized(self, tmp_path):
        """规范化后仍含 .. 也应拒绝"""
        traversing = os.path.join(str(tmp_path), "..", "escape.onnx")
        result = validate_model_path(traversing)
        assert result.ok is False
        assert result.error_code == "ERR_VAI_MODEL_PATH_TRAVERSAL"

    def test_not_in_allowed_dirs(self, tmp_path):
        allowed = [str(tmp_path / "models")]
        result = validate_model_path("/other/place/model.onnx", allowed_dirs=allowed)
        assert result.ok is False
        assert result.error_code == "ERR_VAI_MODEL_PATH_NOT_ALLOWED"

    def test_in_allowed_dir_subpath_passes_dir_check(self, tmp_path):
        """allowed_dirs 子路径通过目录检查（后续仅校验扩展名/文件头）"""
        allowed = [str(tmp_path)]
        result = validate_model_path(str(tmp_path / "sub" / "model.onnx"), allowed_dirs=allowed)
        # 文件不存在 → 跳过文件头检查 → ok
        assert result.ok is True

    def test_allowed_dir_exact_match(self, tmp_path):
        """路径恰好等于 allowed_dir（文件本身）"""
        allowed = [str(tmp_path / "model.onnx")]
        result = validate_model_path(str(tmp_path / "model.onnx"), allowed_dirs=allowed)
        assert result.ok is True

    def test_wrong_extension(self, tmp_path):
        result = validate_model_path(str(tmp_path / "model.pt"))
        assert result.ok is False
        assert result.error_code == "ERR_VAI_MODEL_FORMAT_INVALID"
        assert ".onnx" in result.detail

    def test_uppercase_onnx_extension_accepted(self, tmp_path):
        """.ONNX 大写扩展名也应接受（lower().endswith）"""
        result = validate_model_path(str(tmp_path / "model.ONNX"))
        assert result.ok is True

    def test_valid_file_with_magic_bytes(self, tmp_path):
        """真实文件 + 正确 ONNX magic bytes → ok"""
        model_file = tmp_path / "model.onnx"
        model_file.write_bytes(b"\x08\x07" + b"\x00" * 100)
        result = validate_model_path(str(model_file))
        assert result.ok is True

    def test_valid_file_wrong_header(self, tmp_path):
        """真实文件 + 错误文件头 → ERR_VAI_MODEL_HEADER_INVALID"""
        model_file = tmp_path / "model.onnx"
        model_file.write_bytes(b"XX" + b"\x00" * 100)
        result = validate_model_path(str(model_file))
        assert result.ok is False
        assert result.error_code == "ERR_VAI_MODEL_HEADER_INVALID"
        assert "5858" in result.detail  # b"XX" → hex 5858

    def test_short_file_header_skipped(self, tmp_path):
        """文件内容不足 2 字节 → 不报头错误（len(header) < 2）"""
        model_file = tmp_path / "model.onnx"
        model_file.write_bytes(b"\x08")
        result = validate_model_path(str(model_file))
        assert result.ok is True

    def test_file_read_oserror(self, tmp_path):
        """读取文件抛 OSError → ERR_VAI_MODEL_VALIDATE_FAILED"""
        model_file = tmp_path / "model.onnx"
        model_file.write_bytes(b"\x08\x07")

        real_open = open

        def _raise_oserror(file, mode="r", *args, **kwargs):
            if isinstance(file, str) and file.endswith("model.onnx"):
                raise OSError("permission denied")
            return real_open(file, mode, *args, **kwargs)

        with patch("builtins.open", _raise_oserror):
            result = validate_model_path(str(model_file))
        assert result.ok is False
        assert result.error_code == "ERR_VAI_MODEL_VALIDATE_FAILED"
        assert "Cannot read model file" in result.detail

    def test_nonexistent_file_passes(self, tmp_path):
        """文件不存在 → 跳过文件头检查 → ok（仅校验路径/扩展名）"""
        result = validate_model_path(str(tmp_path / "nonexistent.onnx"))
        assert result.ok is True

    def test_no_allowed_dirs_default_empty(self, tmp_path):
        """allowed_dirs=None 且 _DEFAULT_MODEL_DIRS 为空 → 跳过目录检查"""
        result = validate_model_path(str(tmp_path / "model.onnx"), allowed_dirs=None)
        assert result.ok is True

    def test_empty_allowed_dirs_list_treated_as_no_check(self, tmp_path):
        """空 list 作为 allowed_dirs → falsy → 跳过目录检查"""
        result = validate_model_path(str(tmp_path / "model.onnx"), allowed_dirs=[])
        assert result.ok is True


# --------------------------------------------------------------------------- #
# resolve_device
# --------------------------------------------------------------------------- #


class TestResolveDevice:
    """设备解析：CUDA/TENSORRT 可用性 → 回退 CPU + 降级标志"""

    def test_no_onnxruntime_returns_cpu(self):
        with patch.object(video_ai, "ONNXRUNTIME_AVAILABLE", False):
            assert resolve_device("CUDA") == ("CPU", False)
            assert resolve_device("TENSORRT") == ("CPU", False)
            assert resolve_device("CPU") == ("CPU", False)
            assert resolve_device("anything") == ("CPU", False)

    def test_cuda_available(self):
        with patch.object(video_ai, "ONNXRUNTIME_AVAILABLE", True), patch.object(
            video_ai.ort, "get_available_providers", return_value=["CUDAExecutionProvider", "CPUExecutionProvider"]
        ):
            assert resolve_device("CUDA") == ("CUDA", False)

    def test_cuda_unavailable_degrades_to_cpu(self):
        with patch.object(video_ai, "ONNXRUNTIME_AVAILABLE", True), patch.object(
            video_ai.ort, "get_available_providers", return_value=["CPUExecutionProvider"]
        ):
            device, degraded = resolve_device("CUDA")
            assert device == "CPU"
            assert degraded is True

    def test_tensorrt_available(self):
        with patch.object(video_ai, "ONNXRUNTIME_AVAILABLE", True), patch.object(
            video_ai.ort,
            "get_available_providers",
            return_value=["TensorrtExecutionProvider", "CUDAExecutionProvider", "CPUExecutionProvider"],
        ):
            assert resolve_device("TENSORRT") == ("TENSORRT", False)

    def test_tensorrt_unavailable_degrades_to_cpu(self):
        with patch.object(video_ai, "ONNXRUNTIME_AVAILABLE", True), patch.object(
            video_ai.ort, "get_available_providers", return_value=["CPUExecutionProvider"]
        ):
            device, degraded = resolve_device("TENSORRT")
            assert device == "CPU"
            assert degraded is True

    def test_unknown_device_returns_cpu_not_degraded(self):
        with patch.object(video_ai, "ONNXRUNTIME_AVAILABLE", True), patch.object(
            video_ai.ort, "get_available_providers", return_value=["CPUExecutionProvider"]
        ):
            assert resolve_device("TPU") == ("CPU", False)
            assert resolve_device("") == ("CPU", False)


# --------------------------------------------------------------------------- #
# 数据类
# --------------------------------------------------------------------------- #


class TestModelValidateResult:
    def test_bool_true_when_ok(self):
        assert bool(ModelValidateResult(ok=True)) is True

    def test_bool_false_when_not_ok(self):
        assert bool(ModelValidateResult(ok=False, error_code="ERR_X")) is False

    def test_defaults_empty(self):
        r = ModelValidateResult(ok=True)
        assert r.error_code == ""
        assert r.detail == ""


class TestInferenceOutput:
    def test_ok_true_when_no_error_code(self):
        out = InferenceOutput()
        assert out.ok is True

    def test_ok_false_when_error_code_set(self):
        out = InferenceOutput(error_code="ERR_VAI_MODEL_NOT_LOADED")
        assert out.ok is False

    def test_defaults(self):
        out = InferenceOutput()
        assert out.boxes == []
        assert out.image_shape == [0, 0, 0]
        assert out.object_count == 0
        assert out.confidence_avg == 0.0
        assert out.inference_time_ms == 0.0
        assert out.error_detail == ""


class TestInferenceConfig:
    def test_defaults(self):
        cfg = InferenceConfig()
        assert cfg.model_path == ""
        assert cfg.model_type == InferenceTask.OBJECT_DETECTION
        assert cfg.confidence_threshold == 0.5
        assert cfg.nms_threshold == 0.4
        assert cfg.input_size == (640, 640)
        assert cfg.device == "CPU"
        assert cfg.labels == []
        assert cfg.max_detections == 100
        assert cfg.inference_timeout == 5.0


class TestInferenceTaskEnum:
    def test_values(self):
        assert InferenceTask.OBJECT_DETECTION.value == "object_detection"
        assert InferenceTask.CLASSIFICATION.value == "classification"
        assert InferenceTask.SEGMENTATION.value == "segmentation"
        assert InferenceTask.ANOMALY_DETECTION.value == "anomaly_detection"
        assert InferenceTask.CUSTOM.value == "custom"


class TestDetectionResult:
    def test_required_fields(self):
        d = DetectionResult(class_id="0", class_name="person", confidence=0.9, bbox=(1, 2, 3, 4))
        assert d.class_id == "0"
        assert d.class_name == "person"
        assert d.confidence == 0.9
        assert d.bbox == (1, 2, 3, 4)
        assert d.mask is None
        assert d.timestamp > 0


# --------------------------------------------------------------------------- #
# VideoAIModel
# --------------------------------------------------------------------------- #


class TestVideoAIModel:
    def test_load_simulation_mode_no_onnxruntime(self):
        """ONNXRUNTIME_AVAILABLE=False → 仿真模式，load 返回 True"""
        cfg = InferenceConfig(model_path="/fake/model.onnx")
        model = VideoAIModel(cfg)
        with patch.object(video_ai, "ONNXRUNTIME_AVAILABLE", False):
            assert model.load() is True
            assert model._initialized is True

    def test_load_simulation_mode_empty_model_path(self):
        """model_path 为空 → 仿真模式"""
        cfg = InferenceConfig(model_path="")
        model = VideoAIModel(cfg)
        with patch.object(video_ai, "ONNXRUNTIME_AVAILABLE", True), patch.object(
            video_ai.ort, "get_available_providers", return_value=["CPUExecutionProvider"]
        ):
            assert model.load() is True
            assert model._initialized is True
            assert model._session is None

    def test_load_real_model_failure_returns_false(self):
        """ONNX 可用但加载模型抛异常 → load 返回 False"""
        cfg = InferenceConfig(model_path="/fake/model.onnx")
        model = VideoAIModel(cfg)
        with patch.object(video_ai, "ONNXRUNTIME_AVAILABLE", True), patch.object(
            video_ai.ort, "get_available_providers", return_value=["CPUExecutionProvider"]
        ), patch.object(video_ai.ort, "InferenceSession", side_effect=RuntimeError("bad model")):
            assert model.load() is False
            assert model._initialized is False
            assert model._session is None

    def test_unload_clears_state(self):
        cfg = InferenceConfig(model_path="/fake/model.onnx")
        model = VideoAIModel(cfg)
        model._initialized = True
        model._input_name = "input"
        model._output_names = ["output"]
        model._session = object()  # type: ignore[assignment]
        model.unload()
        assert model._session is None
        assert model._input_name is None
        assert model._output_names == []
        assert model._initialized is False

    @pytest.mark.asyncio
    async def test_infer_not_initialized_returns_empty(self):
        cfg = InferenceConfig()
        model = VideoAIModel(cfg)
        model._initialized = False
        image = np.zeros((10, 10, 3), dtype=np.uint8)
        results = await model.infer(image)
        assert results == []

    @pytest.mark.asyncio
    async def test_infer_structured_not_initialized(self):
        cfg = InferenceConfig()
        model = VideoAIModel(cfg)
        model._initialized = False
        out = await model.infer_structured(np.zeros((10, 10, 3), dtype=np.uint8))
        assert out.ok is False
        assert out.error_code == "ERR_VAI_MODEL_NOT_LOADED"

    @pytest.mark.asyncio
    async def test_infer_structured_none_image(self):
        cfg = InferenceConfig()
        model = VideoAIModel(cfg)
        model._initialized = True
        out = await model.infer_structured(None)  # type: ignore[arg-type]
        assert out.ok is False
        assert out.error_code == "ERR_VAI_PREPROCESS_FAILED"

    @pytest.mark.asyncio
    async def test_infer_structured_empty_image(self):
        cfg = InferenceConfig()
        model = VideoAIModel(cfg)
        model._initialized = True
        out = await model.infer_structured(np.array([]))
        assert out.ok is False
        assert out.error_code == "ERR_VAI_PREPROCESS_FAILED"

    def test_nms_empty_boxes(self):
        cfg = InferenceConfig()
        model = VideoAIModel(cfg)
        empty = np.array([], dtype=np.float64).reshape(0, 4)
        scores = np.array([], dtype=np.float64)
        keep = model._nms(empty, scores)
        assert len(keep) == 0

    def test_nms_keeps_non_overlapping(self):
        cfg = InferenceConfig(nms_threshold=0.5)
        model = VideoAIModel(cfg)
        boxes = np.array([[0, 0, 10, 10], [100, 100, 110, 110]], dtype=np.float64)
        scores = np.array([0.9, 0.8], dtype=np.float64)
        keep = model._nms(boxes, scores)
        assert len(keep) == 2  # 无重叠，全部保留

    def test_nms_suppresses_overlapping(self):
        cfg = InferenceConfig(nms_threshold=0.1)
        model = VideoAIModel(cfg)
        boxes = np.array([[0, 0, 10, 10], [1, 1, 11, 11]], dtype=np.float64)
        scores = np.array([0.9, 0.8], dtype=np.float64)
        keep = model._nms(boxes, scores)
        assert len(keep) == 1  # 高度重叠，仅保留高分框
