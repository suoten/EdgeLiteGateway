"""协议数据包记录器 — 中立模块，无 API 层依赖。

FIXED(架构P0): 原问题-record_packet 定义在 edgelite.api.debug 中，该模块在模块级
导入 FastAPI (APIRouter, Depends, HTTPException 等)。engine 层和 drivers 层通过
`from edgelite.api.debug import record_packet` 引入此函数时，会强制加载整个 FastAPI
API 栈，导致：
1. 无 API 层的纯采集模式（嵌入式部署）下 ImportError
2. engine→api 跨层依赖违反分层架构原则
3. 循环导入风险（若 edgelite.api.debug 初始化失败，整个 AI 推理引擎无法加载）

本模块仅依赖标准库 (itertools, time, collections.deque)，engine/drivers/api 三层
均可安全导入。

使用方式：
    from edgelite.packet_recorder import record_packet
    record_packet("tx", "modbus_tcp", "device_1", "010300000001")
"""

from __future__ import annotations

import itertools
import logging
import time
from collections import deque
from typing import Any

logger = logging.getLogger(__name__)

# 最大缓冲区大小（每个协议一个 deque）
MAX_PACKET_BUFFER = 1000

# 全局数据包缓冲区：protocol_key → deque[packet_dict]
# "__all__" key 存储所有协议的混合包
_packet_buffers: dict[str, deque[dict[str, Any]]] = {}

# 单调递增的全局包序列号。用于 monitor 跟踪已发送位置，
# 避免 deque(maxlen=N) 滚动后 len(buf) 恒等于 maxlen 导致漏发/重发。
_packet_seq = itertools.count(1)


def get_buffer(protocol: str | None = None) -> deque[dict[str, Any]]:
    """获取或创建指定协议的数据包缓冲区。

    Args:
        protocol: 协议标识 (如 "modbus_tcp")，None 或空字符串表示全局缓冲区

    Returns:
        该协议的 deque 缓冲区 (maxlen=MAX_PACKET_BUFFER)
    """
    key = protocol or "__all__"
    if key not in _packet_buffers:
        _packet_buffers[key] = deque(maxlen=MAX_PACKET_BUFFER)
    return _packet_buffers[key]


def record_packet(
    direction: str,
    protocol: str,
    device_id: str,
    content: str | bytes,
    metadata: dict[str, Any] | None = None,
) -> None:
    """记录一个协议数据包（供抓包/嗅探功能使用）。由 driver 层调用。

    Args:
        direction: 传输方向，"tx" (发送) 或 "rx" (接收)
        protocol: 协议标识 (如 "modbus_tcp", "opcua")
        device_id: 设备 ID
        content: 数据包内容，str 或 bytes (bytes 会被转为 hex 字符串)
        metadata: 附加元数据 (如寄存器地址、功能码等)
    """
    packet = {
        "seq": next(_packet_seq),
        "timestamp": time.time(),
        "direction": direction,
        "protocol": protocol,
        "device_id": device_id,
        "content": content if isinstance(content, str) else content.hex(),
        "content_type": "hex" if isinstance(content, bytes) else "ascii",
        "metadata": metadata or {},
    }
    get_buffer(protocol).append(packet)
    get_buffer("__all__").append(packet)


def clear_buffer(protocol: str | None = None) -> int:
    """清空指定协议（或全局）的数据包缓冲区。

    Args:
        protocol: 协议标识，None 表示清空全局缓冲区

    Returns:
        被清除的数据包数量
    """
    key = protocol or "__all__"
    if key in _packet_buffers:
        count = len(_packet_buffers[key])
        _packet_buffers[key].clear()
        return count
    return 0


def get_all_buffers() -> dict[str, deque[dict[str, Any]]]:
    """返回所有协议缓冲区的引用（供 API 层 WebSocket monitor 等使用）。"""
    return _packet_buffers
