"""趋势预测自学习器：从历史时序数据学习线性趋势，生成 ONNX 预测模型

FIXED: 2026-06-30 项2 - 自学习器 ONNX 回写闭环

算法：线性回归（最小二乘）
1. 从历史时序构建训练对：(输入窗口[200], 目标值[10])
2. 使用 numpy.linalg.lstsq 求解 W 和 b: output = input @ W + b
3. 构造 ONNX 图：MatMul → Add（与预置模型结构一致）

ONNX 图结构（opset 13）：
  input → MatMul(W) → Add(b) → output

输入：[1, 200] 历史窗口值
输出：[1, 10] 未来 10 步预测
"""

from __future__ import annotations

import logging
from typing import Any

from edgelite.engine.self_learner_base import SelfLearnerBase

logger = logging.getLogger(__name__)

MODEL_ID = "elg-trend-v1"
MODEL_FILE = "elg-trend-v1.onnx"
INPUT_DIM = 200
OUTPUT_DIM = 10
_INPUT_SHAPE = [1, INPUT_DIM]
_OUTPUT_SHAPE = [1, OUTPUT_DIM]


class TrendSelfLearner(SelfLearnerBase):
    """趋势预测自学习器

    从历史时序数据学习线性回归模型，预测未来趋势。
    使用最小二乘法拟合 W 和 b，构造 ONNX 线性预测模型。

    使用方式：
        learner = TrendSelfLearner(models_dir="/path/to/models")
        # 添加 (window, next_values) 训练对
        learner.add_sample({"window": np_array_200, "target": np_array_10})
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
        """从时序数据拟合线性回归 W 和 b

        data: 样本列表，每个样本为 {"window": [in_dim], "target": [out_dim]} 或 [in_dim] 数组
        """
        try:
            import numpy as np
        except ImportError:
            logger.error("numpy not installed, cannot train trend learner")
            return None

        # 解析训练数据
        X_list = []
        Y_list = []
        for sample in data:
            if isinstance(sample, dict):
                window = sample.get("window")
                target = sample.get("target")
                if window is None or target is None:
                    continue
            elif isinstance(sample, (list, tuple)) and len(sample) == 2:
                window, target = sample
            else:
                # 单数组：假设前 INPUT_DIM 为窗口，后 OUTPUT_DIM 为目标
                arr = np.array(sample, dtype=np.float32)
                if arr.shape[0] >= INPUT_DIM + OUTPUT_DIM:
                    window = arr[:INPUT_DIM]
                    target = arr[INPUT_DIM : INPUT_DIM + OUTPUT_DIM]
                else:
                    continue

            try:
                w = np.array(window, dtype=np.float32).flatten()
                t = np.array(target, dtype=np.float32).flatten()
            except (ValueError, TypeError):
                continue

            if w.shape[0] != INPUT_DIM or t.shape[0] != OUTPUT_DIM:
                # 截断或填充
                w = self._adjust_dim(w, INPUT_DIM)
                t = self._adjust_dim(t, OUTPUT_DIM)

            X_list.append(w)
            Y_list.append(t)

        if not X_list:
            logger.warning("Trend training: no valid samples parsed")
            return None

        X = np.array(X_list, dtype=np.float32)  # [N, in_dim]
        Y = np.array(Y_list, dtype=np.float32)  # [N, out_dim]

        # 最小二乘拟合: X @ W + b ≈ Y
        # 增广 X 加入偏置列: [N, in_dim+1]
        ones = np.ones((X.shape[0], 1), dtype=np.float32)
        X_aug = np.hstack([X, ones])  # [N, in_dim+1]

        try:
            # lstsq 返回 (解, 残差, 秩, 奇异值)
            solution, _, _, _ = np.linalg.lstsq(X_aug, Y, rcond=None)
            W = solution[:INPUT_DIM]  # [in_dim, out_dim]
            b = solution[INPUT_DIM]  # [out_dim]
        except np.linalg.LinAlgError as e:
            logger.error("Trend lstsq failed: %s", e)
            return None

        return {
            "W": W.astype(np.float32),
            "b": b.astype(np.float32),
        }

    @staticmethod
    def _adjust_dim(arr: Any, target_dim: int) -> Any:
        """调整数组维度到目标长度（截断或零填充）"""
        import numpy as np

        if arr.shape[0] > target_dim:
            return arr[:target_dim]
        elif arr.shape[0] < target_dim:
            pad = np.zeros(target_dim - arr.shape[0], dtype=arr.dtype)
            return np.hstack([arr, pad])
        return arr

    def _build_onnx_graph(self, weights: dict[str, Any]) -> bytes | None:
        """构造线性回归 ONNX 图（MatMul → Add）"""
        try:
            import onnx
            from onnx import TensorProto, helper, numpy_helper
        except ImportError:
            logger.error("onnx/numpy not installed, cannot build trend ONNX graph")
            return None

        W = weights["W"]  # [in_dim, out_dim]
        b = weights["b"]  # [out_dim]

        X = helper.make_tensor_value_info("input", TensorProto.FLOAT, _INPUT_SHAPE)
        Y = helper.make_tensor_value_info("output", TensorProto.FLOAT, _OUTPUT_SHAPE)

        nodes = [
            helper.make_node("MatMul", ["input", "W"], ["linear_out"]),
            helper.make_node("Add", ["linear_out", "b"], ["output"]),
        ]

        inits = [
            numpy_helper.from_array(W, name="W"),
            numpy_helper.from_array(b, name="b"),
        ]

        graph = helper.make_graph(nodes, "trend_trained", [X], [Y], initializer=inits)
        model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
        model.ir_version = 7
        model.model_version = 1
        model.doc_string = f"{MODEL_FILE}: linear regression trend predictor"

        try:
            onnx.checker.check_model(model)
        except Exception as e:
            logger.warning("ONNX model validation failed for %s: %s", MODEL_FILE, e)
        return model.SerializeToString()

    def _default_weights(self) -> dict[str, Any]:
        """返回默认（未训练）权重：W=0, b=0"""
        import numpy as np

        return {
            "W": np.zeros((INPUT_DIM, OUTPUT_DIM), dtype=np.float32),
            "b": np.zeros(OUTPUT_DIM, dtype=np.float32),
        }
