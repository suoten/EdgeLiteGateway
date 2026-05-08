"""Modbus Slave数据映射 - 纯Python回退实现

当Cython编译的_cython.modbus_mapper不可用时使用此模块。
功能完全相同，只是没有C层加速。
"""

import struct


def map_device_data_fast(
    points: dict, holding_regs: list, input_regs: list, coils: list, base_address: int = 0
) -> int:
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
            if offset < len(holding_regs):
                holding_regs[offset] = hi
            if offset + 1 < len(holding_regs):
                holding_regs[offset + 1] = lo
            offset += 2
        elif isinstance(value, int):
            if 0 <= value <= 65535:
                if offset < len(holding_regs):
                    holding_regs[offset] = value
                offset += 1
            else:
                hi = (value >> 16) & 0xFFFF
                lo = value & 0xFFFF
                if offset < len(holding_regs):
                    holding_regs[offset] = hi
                if offset + 1 < len(holding_regs):
                    holding_regs[offset + 1] = lo
                offset += 2
        else:
            offset += 1

    return offset
