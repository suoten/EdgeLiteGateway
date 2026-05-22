from __future__ import annotations

import sys
from pathlib import Path

try:
    import numpy as np
    import onnx
    from onnx import TensorProto, helper, numpy_helper
except ImportError:
    print("onnx/numpy库未安装，跳过模型生成。请运行: pip install onnx numpy")
    sys.exit(0)


def _make_identity_model(name: str, version: str) -> bytes:
    X = helper.make_tensor_value_info("input", TensorProto.FLOAT, [1])
    Y = helper.make_tensor_value_info("output", TensorProto.FLOAT, [1])
    identity_node = helper.make_node("Identity", inputs=["input"], outputs=["output"])
    graph = helper.make_graph([identity_node], f"{name}_graph", [X], [Y])
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 7
    model.model_version = 1
    model.doc_string = f"{name} v{version}"
    onnx.checker.check_model(model)
    return model.SerializeToString()


def _make_linear_model(name: str, version: str, input_shape: list[int], output_shape: list[int]) -> bytes:
    """生成 y = 0.01 * W @ x + b 的线性模型，W用小随机正交矩阵+偏置"""
    in_dim = input_shape[-1]
    out_dim = output_shape[-1]

    rng = np.random.RandomState(42)
    W = rng.randn(in_dim, out_dim).astype(np.float32) * 0.01
    b = rng.randn(out_dim).astype(np.float32) * 0.1 + 0.5

    W_init = numpy_helper.from_array(W, name="W")
    b_init = numpy_helper.from_array(b, name="b")

    X = helper.make_tensor_value_info("input", TensorProto.FLOAT, input_shape)
    Y = helper.make_tensor_value_info("output", TensorProto.FLOAT, output_shape)

    matmul_node = helper.make_node("MatMul", inputs=["input", "W"], outputs=["matmul_out"])
    add_node = helper.make_node("Add", inputs=["matmul_out", "b"], outputs=["output"])

    graph = helper.make_graph(
        [matmul_node, add_node],
        f"{name}_graph",
        [X],
        [Y],
        initializer=[W_init, b_init],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 7
    model.model_version = 1
    model.doc_string = f"{name} v{version}"
    onnx.checker.check_model(model)
    return model.SerializeToString()


def main() -> None:
    out_dir = Path(__file__).parent
    presets = [
        ("elg-anomaly-v1", "1.0.0", "anomaly detection"),
        ("elg-trend-v1", "1.0.0", "trend prediction"),
        ("elg-threshold-v1", "1.0.0", "dynamic threshold"),
    ]
    for filename, version, desc in presets:
        data = _make_identity_model(filename, version)
        path = out_dir / f"{filename}.onnx"
        path.write_bytes(data)
        print(f"生成: {path} ({len(data)} bytes)")

    linear_models = [
        ("elg-vibration-v1", "1.0.0", [1, 128], [1, 2], "vibration classification + anomaly score"),
        ("elg-power-v1", "1.0.0", [1, 168], [1, 24], "24h energy consumption forecast"),
        ("elg-quality-v1", "1.0.0", [1, 50], [1, 1], "quality score 0-100"),
        ("elg-battery-v1", "1.0.0", [1, 100], [1, 1], "SOH percentage"),
        ("elg-leak-v1", "1.0.0", [1, 60], [1, 1], "leak probability 0-1"),
    ]
    for filename, version, in_shape, out_shape, desc in linear_models:
        data = _make_linear_model(filename, version, in_shape, out_shape)
        path = out_dir / f"{filename}.onnx"
        path.write_bytes(data)
        print(f"生成: {path} ({len(data)} bytes) - {desc}")


if __name__ == "__main__":
    main()
