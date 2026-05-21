from __future__ import annotations

import sys
from pathlib import Path

try:
    import onnx
    from onnx import TensorProto, helper
except ImportError:
    print("onnx库未安装，跳过模型生成。请运行: pip install onnx")
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


if __name__ == "__main__":
    main()
