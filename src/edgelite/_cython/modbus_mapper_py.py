"""Modbus Slave数据映射 - 纯Python回退实现

当Cython编译的_cython.modbus_mapper不可用时使用此模块。
功能完全相同，只是没有C层加速。
"""

import struct

MODBUS_EXCEPTION_CN = {
    0x01: "非法功能码",
    0x02: "非法数据地址",
    0x03: "非法数据值",
    0x04: "从站设备故障",
    0x05: "确认",
    0x06: "从站设备忙",
    0x07: "否定确认",
    0x08: "内存奇偶错误",
    0x0A: "网关路径不可用",
    0x0B: "网关目标设备失败",
    0x2B: "扩展异常",
}


def map_device_data_fast(points: dict, holding_regs: list, input_regs: list, coils: list, base_address: int = 0) -> int:
    """将设备测点数据映射到Modbus寄存器（纯Python版）"""
    offset = base_address

    for _key, value in points.items():
        if isinstance(value, bool):
            if offset < len(coils):
                coils[offset] = int(value)
            offset += 1
        elif isinstance(value, float):
            raw = struct.pack(">f", value)
            hi = struct.unpack(">H", raw[:2])[0]
            lo = struct.unpack(">H", raw[2:])[0]
            # FIXED-P1: 原问题-仅检查 offset 和 offset+1 各自是否越界，若 offset 在末尾，
            # 只写 hi 不写 lo 导致浮点数损坏；改为同时检查两个寄存器是否可写
            if offset + 1 < len(holding_regs):
                holding_regs[offset] = hi
                holding_regs[offset + 1] = lo
            offset += 2
        elif isinstance(value, int):
            if -32768 <= value <= 65535:
                if value < 0:
                    value = value & 0xFFFF  # int16补码
                if offset < len(holding_regs):
                    holding_regs[offset] = value
                offset += 1
            elif -2147483648 <= value <= 4294967295:
                if value < 0:
                    value = value & 0xFFFFFFFF  # int32补码
                hi = (value >> 16) & 0xFFFF
                lo = value & 0xFFFF
                # FIXED-P1: 原问题-同浮点数，32位整数也需要同时检查两个寄存器
                if offset + 1 < len(holding_regs):
                    holding_regs[offset] = hi
                    holding_regs[offset + 1] = lo
                offset += 2
        else:
            offset += 1

    return offset
