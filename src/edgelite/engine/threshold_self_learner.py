"""动态阈值自学习器：从历史数据学习统计阈值，生成 ONNX 阈值计算模型

FIXED: 2026-06-30 项2 - 自学习器 ONNX 回写闭环

算法：动态统计阈值（mean + k * std）
1. 从历史数据学习 k 值（基于数据分布特征）
2. 构造 ONNX 图：从输入窗口动态计算 mean + k * std
3. 输出最优阈值，用于告警判定

ONNX 图结构（opset 13）：
  input → ReduceMean → mean_val
  input → Sub(mean_val) → diff → Mul(自身) → sq → ReduceMean → var
  var → Mul(k²) → Sqrt → k_std
  mean_val + k_std → output

输入：[1, 50] 历史值窗口
输出：[1, 1] 动态阈值

特点：阈值随输入窗口动态变化，而非固定值。
k 值由训练数据决定（默认 3.0 = 3-sigma 规则）。
"""

from __future__ import annotations

import logging
from typing import Any

from edgelite.engine.self_learner_base import SelfLearnerBase

logger = logging.getLogger(__name__)

MODEL_ID = "elg-threshold-v1"
MODEL_FILE = "elg-threshold-v1.onnx"
INPUT_DIM = 50
OUTPUT_DIM = 1
_INPUT_SHAPE = [1, INPUT_DIM]
_OUTPUT_SHAPE = [1, OUTPUT_DIM]
_DEFAULT_K = 3.0  # 默认 3-sigma


class ThresholdSelfLearner(SelfLearnerBase):
    """动态阈值自学习器

    从历史数据学习最优 k 值（敏感度系数），构造 ONNX 动态阈值模型。
    模型在推理时从输入窗口实时计算 mean + k * std 作为阈值。

    k 值学习策略：
    - 收集正常数据，计算各窗口的 mean 和 std
    - 若数据离散度低（std/mean < 0.1），使用更严格的 k=2.0
    - 若数据离散度高（std/mean > 0.5），使用更宽松的 k=4.0
    - 默认 k=3.0（3-sigma 规则）

    使用方式：
        learner = ThresholdSelfLearner(models_dir="/path/to/models")
        learner.add_sample(np_array_of_50_values)
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
        """从历史数据学习 k 值

        data: 样本列表，每个样本为 [in_dim] 的数值数组
        """
        try:
            import numpy as np
        except ImportError:
            logger.error("numpy not installed, cannot train threshold learner")
            return None

        try:
            matrix = np.array(data, dtype=np.float32)
        except (ValueError, TypeError) as e:
            logger.error("Threshold training data conversion failed: %s", e)
            return None

        if matrix.ndim == 1:
            matrix = matrix.reshape(1, -1)
        if matrix.shape[1] != INPUT_DIM:
            if matrix.shape[1] > INPUT_DIM:
                matrix = matrix[:, :INPUT_DIM]
            else:
                pad = np.zeros((matrix.shape[0], INPUT_DIM - matrix.shape[1]), dtype=np.float32)
                matrix = np.hstack([matrix, pad])

        # 计算各窗口的变异系数 (CV = std / |mean|)
        means = np.mean(matrix, axis=1)  # [N]
        stds = np.std(matrix, axis=1)    # [N]
        # 避免除零
        cv = stds / (np.abs(means) + 1e-8)
        mean_cv = float(np.mean(cv))

        # 根据变异系数调整 k 值
        if mean_cv < 0.1:
            k = 2.0  # 数据稳定，使用严格阈值
        elif mean_cv > 0.5:
            k = 4.0  # 数据波动大，使用宽松阈值
        else:
            k = _DEFAULT_K  # 3-sigma

        # 存储为 k² （ONNX 中用 var * k² 然后 Sqrt 得到 k * std）
        k_squared = np.array([k * k], dtype=np.float32)

        logger.info(
            "%s: trained k=%.1f (mean_cv=%.3f, samples=%d)",
            MODEL_ID, k, mean_cv, len(data),
        )

        return {
            "k_squared": k_squared,
            "k": k,  # 元数据，不参与 ONNX 构造
        }

    def _build_onnx_graph(self, weights: dict[str, Any]) -> bytes | None:
        """构造动态统计阈值 ONNX 图

        图计算：
          mean_val = ReduceMean(input, axes=[1])       # [1, 1]
          diff = input - mean_val                       # [1, in_dim] (广播)
          sq = diff * diff                              # [1, in_dim]
          var = ReduceMean(sq, axes=[1])                # [1, 1]
          k_std = Sqrt(var * k_squared)                # [1, 1] = k * std
          output = mean_val + k_std                    # [1, 1]
        """
        try:
            import onnx
            from onnx import TensorProto, helper, numpy_helper
        except ImportError:
            logger.error("onnx/numpy not installed, cannot build threshold ONNX graph")
            return None

        k_squared = weights["k_squared"]  # [1]

        X = helper.make_tensor_value_info("input", TensorProto.FLOAT, _INPUT_SHAPE)
        Y = helper.make_tensor_value_info("output", TensorProto.FLOAT, _OUTPUT_SHAPE)

        nodes = [
            # mean_val = mean(input, axis=1) -> [1, 1]
            helper.make_node("ReduceMean", ["input"], ["mean_val"], axes=[1]),
            # diff = input - mean_val (broadcast [1, in_dim] - [1, 1] -> [1, in_dim])
            helper.make_node("Sub", ["input", "mean_val"], ["diff"]),
            # sq = diff * diff
            helper.make_node("Mul", ["diff", "diff"], ["sq"]),
            # var = mean(sq, axis=1) -> [1, 1]
            helper.make_node("ReduceMean", ["sq"], ["var"], axes=[1]),
            # k_var = var * k_squared
            helper.make_node("Mul", ["var", "k_squared"], ["k_var"]),
            # k_std = sqrt(k_var) = k * std
            helper.make_node("Sqrt", ["k_var"], ["k_std"]),
            # output = mean_val + k_std
            helper.make_node("Add", ["mean_val", "k_std"], ["output"]),
        ]

        inits = [
            numpy_helper.from_array(k_squared, name="k_squared"),
        ]

        graph = helper.make_graph(nodes, "threshold_trained", [X], [Y], initializer=inits)
        model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
        model.ir_version = 7
        model.model_version = 1
        model.doc_string = f"{MODEL_FILE}: dynamic statistical threshold (mean + k*std)"

        try:
            onnx.checker.check_model(model)
        except Exception as e:
            logger.warning("ONNX model validation failed for %s: %s", MODEL_FILE, e)
        return model.SerializeToString()

    def _default_weights(self) -> dict[str, Any]:
        """返回默认（未训练）权重：k²=9.0（k=3.0）"""
        import numpy as np
        return {
            "k_squared": np.array([9.0], dtype=np.float32),
            "k": _DEFAULT_K,
        }
