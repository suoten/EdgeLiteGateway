"""异常检测自学习器：从正常运行数据学习统计基线，生成 ONNX 异常评分模型

FIXED: 2026-06-30 项2 - 自学习器 ONNX 回写闭环

算法：统计 z-score 异常评分
1. 从正常数据计算各特征均值 (mean) 和标准差 (std)
2. 构造 ONNX 图：z = (input - mean) / std → z² → ReduceMean → Sigmoid
3. 异常分数 = sigmoid(mean(z²) * scale)，范围 [0, 1]
4. 正常数据 z 接近 0 → score 接近 0.5 以下；异常数据 z 大 → score 接近 1

ONNX 图结构（opset 13）：
  input → Sub(mean) → Div(std) → Mul(自身) → ReduceMean(axes=[1]) → Mul(scale) → Sigmoid → output

输入：[1, 100] 特征向量（设备点位值）
输出：[1, 1] 异常分数（0-1）
"""

from __future__ import annotations

import logging
from typing import Any

from edgelite.engine.self_learner_base import SelfLearnerBase

logger = logging.getLogger(__name__)

# 模型元数据（与 edge_ai_inference.PRESET_MODELS 保持一致）
MODEL_ID = "elg-anomaly-v1"
MODEL_FILE = "elg-anomaly-v1.onnx"
INPUT_DIM = 100
OUTPUT_DIM = 1
_INPUT_SHAPE = [1, INPUT_DIM]
_OUTPUT_SHAPE = [1, OUTPUT_DIM]


class AnomalySelfLearner(SelfLearnerBase):
    """异常检测自学习器

    从正常运行数据学习统计基线（mean, std），构造 ONNX 异常评分模型。
    当设备点位值偏离基线时，异常分数升高。

    使用方式：
        learner = AnomalySelfLearner(models_dir="/path/to/models")
        learner.add_sample(np_array_of_100_features)
        result = await learner.train_and_export()
    """

    def __init__(self, models_dir: str, ai_engine: Any = None, **kwargs: Any):
        super().__init__(
            model_id=MODEL_ID,
            model_file=MODEL_FILE,
            models_dir=models_dir,
            ai_engine=ai_engine,
            **kwargs,
        )

    def _train(self, data: list[Any]) -> dict[str, Any] | None:
        """从正常数据计算 mean 和 std

        data: 样本列表，每个样本为 [in_dim] 的数值数组
        """
        try:
            import numpy as np
        except ImportError:
            logger.error("numpy not installed, cannot train anomaly learner")
            return None

        # 将样本堆叠为 [N, in_dim] 矩阵
        try:
            matrix = np.array(data, dtype=np.float32)
        except (ValueError, TypeError) as e:
            logger.error("Anomaly training data conversion failed: %s", e)
            return None

        if matrix.ndim == 1:
            matrix = matrix.reshape(1, -1)
        if matrix.shape[1] != INPUT_DIM:
            # 截断或填充到 INPUT_DIM
            if matrix.shape[1] > INPUT_DIM:
                matrix = matrix[:, :INPUT_DIM]
            else:
                pad = np.zeros((matrix.shape[0], INPUT_DIM - matrix.shape[1]), dtype=np.float32)
                matrix = np.hstack([matrix, pad])

        # 计算统计量
        mean = np.mean(matrix, axis=0)  # [in_dim]
        std = np.std(matrix, axis=0)  # [in_dim]
        # 防止除零：std 最小 1e-8
        std = np.maximum(std, 1e-8)
        # scale: 将 mean(z²) 映射到合理范围
        # 正常数据 mean(z²) ≈ 1.0，sigmoid(1.0 * scale) 应在 0.3-0.7
        # scale = 1.0 使 sigmoid(1.0) ≈ 0.73，sigmoid(4.0) ≈ 0.98（明显异常）
        scale = np.array([1.0], dtype=np.float32)

        return {
            "mean": mean.astype(np.float32),
            "std": std.astype(np.float32),
            "scale": scale,
        }

    def _build_onnx_graph(self, weights: dict[str, Any]) -> bytes | None:
        """构造统计 z-score 异常评分 ONNX 图"""
        try:
            import onnx
            from onnx import TensorProto, helper, numpy_helper
        except ImportError:
            logger.error("onnx/numpy not installed, cannot build anomaly ONNX graph")
            return None

        mean = weights["mean"]
        std = weights["std"]
        scale = weights["scale"]

        X = helper.make_tensor_value_info("input", TensorProto.FLOAT, _INPUT_SHAPE)
        Y = helper.make_tensor_value_info("output", TensorProto.FLOAT, _OUTPUT_SHAPE)

        nodes = [
            helper.make_node("Sub", ["input", "mean"], ["centered"]),
            helper.make_node("Div", ["centered", "std"], ["z"]),
            helper.make_node("Mul", ["z", "z"], ["z_sq"]),
            helper.make_node("ReduceMean", ["z_sq"], ["mean_z_sq"], axes=[1]),
            helper.make_node("Mul", ["mean_z_sq", "scale"], ["scaled"]),
            helper.make_node("Sigmoid", ["scaled"], ["output"]),
        ]

        inits = [
            numpy_helper.from_array(mean, name="mean"),
            numpy_helper.from_array(std, name="std"),
            numpy_helper.from_array(scale, name="scale"),
        ]

        graph = helper.make_graph(nodes, "anomaly_trained", [X], [Y], initializer=inits)
        model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
        model.ir_version = 7
        model.model_version = 1
        model.doc_string = f"{MODEL_FILE}: statistical z-score anomaly scorer"

        try:
            onnx.checker.check_model(model)
        except Exception as e:
            logger.warning("ONNX model validation failed for %s: %s", MODEL_FILE, e)
        return model.SerializeToString()

    def _default_weights(self) -> dict[str, Any]:
        """返回默认（未训练）权重：mean=0, std=1, scale=1"""
        import numpy as np

        return {
            "mean": np.zeros(INPUT_DIM, dtype=np.float32),
            "std": np.ones(INPUT_DIM, dtype=np.float32),
            "scale": np.array([1.0], dtype=np.float32),
        }
