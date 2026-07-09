"""协议键名归一化模块（单一数据源）

R11-DRV-07: 作为协议别名的单一数据源，复用 constants.py 中的
``_PROTOCOL_ALIASES`` 表与 ``normalize_protocol`` 函数，
避免两套并行系统不一致。

所有 API 层（debug / drivers 等）应从本模块导入，而非直接从 constants 导入，
以便未来扩展（如动态注册自定义协议别名）时只需修改一处。
"""

from __future__ import annotations

from edgelite.constants import _PROTOCOL_ALIASES, normalize_protocol

# 对外暴露的别名表（只读视图，防止外部直接修改 constants 内部表）
protocol_key_aliases: dict[str, str] = dict(_PROTOCOL_ALIASES)


def normalize_protocol_key(key: str) -> str | None:
    """将协议键名归一化为规范形式。

    接受旧式连字符风格（如 ``modbus-tcp``）和短名（如 ``s7``、``ab``），
    返回规范的下划线风格名称（如 ``modbus_tcp``、``siemens_s7``）。

    Args:
        key: 原始协议键名（可能为旧式别名）。

    Returns:
        规范协议名；若 ``key`` 不在已知协议列表且非已知别名则返回 ``None``。
    """
    if not key:
        return None
    return normalize_protocol(key)


__all__ = ["normalize_protocol_key", "protocol_key_aliases"]
