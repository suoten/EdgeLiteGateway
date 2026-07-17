"""Modbus TCP/RTU 共享模块 — 提取自 modbus_tcp.py 和 modbus_rtu.py 的公共逻辑

FIXED-P2 (Task #17): 消除 modbus_tcp.py (2534行) 和 modbus_rtu.py (2695行) 之间
约 300 行重复代码，统一异常码映射、寄存器类型、字节序和数据类型定义。

共享内容:
  - pymodbus 版本检测 (_PYMODBUS_MAJOR / _PYMODBUS_MINOR)
  - slave 参数名检测 (_detect_slave_kwarg_name / _slave_kwarg / _set_client_slave_id)
  - Modbus 异常码映射 (_MODBUS_EXCEPTION_CODES + _parse_modbus_exception)
  - 寄存器类型映射 (REGISTER_TYPES / DATA_TYPE_REGS / _BYTE_ORDER_FMT)

改进:
  - _parse_modbus_exception 对未映射的异常码返回 "Unknown Exception (0xNN)" 而非 None
  - _slave_kwarg 新增 allow_broadcast 参数 (TCP 允许 slave_id=0 广播)
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# ── pymodbus 版本检测 ──────────────────────────────────────────────────
# modbus_rtu.py 中 pymodbus 可能未安装 (try/except)，此处同样容错
try:
    import pymodbus  # type: ignore[import-not-found]

    _PYMODBUS_MAJOR = int(getattr(pymodbus, "__version__", "2.0.0").split(".")[0])
    _PYMODBUS_MINOR = int(getattr(pymodbus, "__version__", "2.0.0").split(".")[1]) if _PYMODBUS_MAJOR >= 3 else 0
except ImportError:
    _PYMODBUS_MAJOR = 3
    _PYMODBUS_MINOR = 0

# _SLAVE_KWARG_NAME: "slave"(pymodbus 2.x) | "unit"(pymodbus 3.0~3.6) | "slave"(pymodbus 3.7+)
# FIXED-MT2-H03: pymodbus 3.7+ 仍接受slave 关键字参数，per-call 传递避免共享连接竞态
_SLAVE_KWARG_NAME: str | None = None

# ── Modbus 异常码映射 (Modbus 协议规范 §6) ────────────────────────────
# 异常码 = 功能码 | 0x80，如 0x83 = 功能码 0x03 (读保持寄存器) 的异常响应
_MODBUS_EXCEPTION_CODES: dict[int, str] = {
    0x81: "Illegal Function (0x01)",
    0x82: "Illegal Data Address (0x02)",
    0x83: "Illegal Data Value (0x03)",
    0x84: "Server Device Failure (0x04)",
    0x85: "Acknowledge (0x05)",
    0x86: "Server Device Busy (0x06)",
    0x87: "Negative Acknowledge (0x07)",
    0x88: "Memory Parity Error (0x08)",
    0x8A: "Gateway Path Unavailable (0x0A)",
    0x8B: "Gateway Target Device Failed (0x0B)",
    0xAB: "Extended Exception (0x2B)",
}


def _detect_slave_kwarg_name() -> str | None:
    """根据pymodbus版本号检测正确的slave参数名称

    pymodbus 2.x: slave
    pymodbus 3.0~3.6: unit
    pymodbus 3.7~3.7.x: slave (per-call传递，避免共享连接slave_id竞态）
    pymodbus 3.8+: device_id (slave 被重命名为 device_id；3.12+ 彻底移除 slave 关键字)
    """
    if _PYMODBUS_MAJOR < 3:
        return "slave"
    if _PYMODBUS_MAJOR == 3 and _PYMODBUS_MINOR < 7:
        return "unit"
    if _PYMODBUS_MAJOR == 3 and _PYMODBUS_MINOR < 8:
        return "slave"
    return "device_id"


def _slave_kwarg(slave_id: int, allow_broadcast: bool = False) -> dict:
    """返回正确的Modbus 设备 ID 参数，所有版本均 per-call 传递

    Args:
        slave_id: Modbus 从站地址 (1-247)
        allow_broadcast: True 允许 slave_id=0 (广播写，仅 TCP 使用)
    """
    if allow_broadcast:
        # TCP 模式: slave_id=0 允许 (广播)，其他值需 1-247
        if slave_id != 0 and not 1 <= slave_id <= 247:
            raise ValueError(f"Modbus config invalid: slave_id must be 0-247, got {slave_id}")
    else:
        # RTU 模式: slave_id 必须 1-247
        if not 1 <= slave_id <= 247:
            raise ValueError(f"Modbus config invalid: slave_id must be 1-247, got {slave_id}")
    global _SLAVE_KWARG_NAME
    if _SLAVE_KWARG_NAME is None:
        _SLAVE_KWARG_NAME = _detect_slave_kwarg_name()
    if _SLAVE_KWARG_NAME is None:
        return {}
    return {_SLAVE_KWARG_NAME: slave_id}


def _set_client_slave_id(client: Any, slave_id: int) -> None:
    """为pymodbus 3.7+ 设置 client.slave_id（现在per-call 传递，此函数为兼容保留）"""
    if _SLAVE_KWARG_NAME is None and hasattr(client, "slave_id"):
        client.slave_id = slave_id


def _read_kwargs(count: int, slave_id: int, allow_broadcast: bool = False) -> dict:
    """返回正确的读取方法关键字参数"""
    kwargs = _slave_kwarg(slave_id, allow_broadcast=allow_broadcast)
    kwargs["count"] = count  # FIXED: 始终传count，旧版本默认count=1导致float32等类型读取不完整
    return kwargs


def _parse_modbus_exception(result: Any) -> str | None:
    """解析Modbus错误响应中的异常码，返回异常码描述

    FIXED-P2 (Task #17): 原代码对未映射的异常码返回 None，调用方无法区分"非异常"和"未知异常码"。
    修复: 未映射的异常码返回 "Unknown Exception (0xNN)" 描述，非异常响应仍返回 None。

    Returns:
        异常码描述字符串 (如 "Illegal Data Address (0x02)")，或 None (非异常响应)
    """
    try:
        raw = getattr(result, "raw", None) or getattr(result, "value", None)
        if raw and isinstance(raw, (bytes, list)):
            data = raw if isinstance(raw, bytes) else bytes(raw)
            if len(data) >= 2:
                exc_code = data[1] | 0x80
                # FIXED-P2: 未映射的异常码返回描述而非 None
                return _MODBUS_EXCEPTION_CODES.get(exc_code, f"Unknown Exception (0x{exc_code & 0x7F:02X})")
        err_str = str(result)
        for code, desc in _MODBUS_EXCEPTION_CODES.items():
            if hex(code) in err_str or desc.split("(")[0].strip() in err_str:
                return desc
        # FIXED-P2: 字符串中包含 "Exception" 但未匹配已知码 → 标记为未知
        if "exception" in err_str.lower() or "error" in err_str.lower():
            return f"Unknown Exception ({err_str[:80]})"
    except Exception as e:
        logger.debug("Failed to parse modbus exception: %s", e)
    return None


# ── 寄存器类型映射 ─────────────────────────────────────────────────────
# 名称 → (功能码, 每个寄存器字节数)
REGISTER_TYPES: dict[str, tuple[int, int]] = {
    "coil": (0, 1),  # 0x01 → read_coils, → write_coil
    "discrete": (1, 1),  # 1x → read_discrete inputs
    "holding": (3, 2),  # 3x → read_holding_registers, → write_register
    "input": (4, 2),  # 4x → read_input_registers
}

# 数据类型→寄存器数量映射
DATA_TYPE_REGS: dict[str, int] = {
    "bool": 1,
    "int16": 1,
    "uint16": 1,
    "int32": 2,
    "uint32": 2,
    "float32": 2,
    "float64": 4,
    "string": 1,  # 每个寄存器2字节
}

# 字节序→(寄存器打包格式, 浮点/整数解包格式, 是否需要字交换)
# FIXED-P1: CDAB/DCBA 需要反转寄存器顺序实现字交换，不能仅靠 struct 端序标志
# 验证: 值 0x12345678 → CDAB 寄存器 [0x5678, 0x1234] → 反转后 [0x1234, 0x5678] → pack('>HH') = b'\x12\x34\x56\x78' → unpack('>I') = 0x12345678 ✓
_BYTE_ORDER_FMT: dict[str, tuple[str, str, bool]] = {
    "ABCD": (">", ">", False),  # Big-Endian (默认), 无字交换
    "BADC": ("<", ">", False),  # Big-Endian Byte Swap, 无字交换
    "CDAB": (">", ">", True),   # Word Swap, 需要反转寄存器顺序
    "DCBA": ("<", ">", True),   # Little-Endian (完全反转), 需要反转寄存器顺序 + 字节内交换
}
