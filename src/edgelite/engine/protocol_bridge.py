"""协议转换网关 - 实现Modbus TCP ↔ OPC UA等协议互转

协议转换网关支持：
- Modbus TCP → OPC UA (作为OPC UA服务器向下游提供数据)
- Modbus TCP → BACnet/IP
- OPC UA Client → MQTT (桥接)
- 通用点映射配置
- 转换规则脚本
- 数据类型转换 (int16→float32等)
"""

from __future__ import annotations

import asyncio
import logging
import struct
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class MappingRule:
    """映射规则"""
    rule_id: str
    source_protocol: str
    target_protocol: str
    source_device: str
    source_point: str
    target_device: str
    target_point: str
    data_type: str = "passthrough"  # passthrough/int16_to_float32/scale/offset
    scale: float = 1.0
    offset: float = 0.0
    enabled: bool = True


@dataclass
class ProtocolBridge:
    """协议桥接器配置"""
    bridge_id: str
    source_protocol: str  # 源协议
    target_protocol: str  # 目标协议
    source_config: dict = field(default_factory=dict)
    target_config: dict = field(default_factory=dict)
    mapping_rules: list[MappingRule] = field(default_factory=list)
    enabled: bool = True


class ProtocolConverter:
    """数据转换器 - 处理数据类型转换"""

    @staticmethod
    def convert(value: Any, conversion_type: str, scale: float = 1.0, offset: float = 0.0) -> float | int | bool:
        """转换数据值

        Args:
            value: 原始值
            conversion_type: 转换类型
            scale: 缩放因子
            offset: 偏移量

        Returns:
            转换后的值
        """
        try:
            # 先转为浮点数
            fvalue = float(value)

            if conversion_type == "passthrough":
                return fvalue

            elif conversion_type == "int16_to_float32":
                # Modbus int16 → float32
                if fvalue >= 32768:
                    fvalue -= 65536
                return fvalue * scale + offset

            elif conversion_type == "uint16_to_float32":
                return fvalue * scale + offset

            elif conversion_type == "scale":
                return fvalue * scale + offset

            elif conversion_type == "offset":
                return fvalue + offset

            elif conversion_type == "invert":
                return -fvalue

            elif conversion_type == "bool_to_int":
                return 1 if fvalue else 0

            elif conversion_type == "bool_to_float":
                return 1.0 if fvalue else 0.0

            elif conversion_type == "percent_to_0_100":
                # 0-27648 (Modbus 典型) → 0-100%
                return (fvalue / 27648.0) * 100.0 * scale + offset

            elif conversion_type == "temperature_pt100":
                # PT100温度传感器 (0-200℃对应0-32767)
                return (fvalue / 32767.0) * 200.0 * scale + offset

            else:
                return fvalue * scale + offset

        except (ValueError, TypeError, ZeroDivisionError) as e:
            logger.warning("数据转换失败: %s, type=%s, error=%s", value, conversion_type, e)
            return 0.0

    @staticmethod
    def reverse_convert(value: Any, conversion_type: str, scale: float = 1.0, offset: float = 0.0) -> int | float:
        """反向转换 (写入时)"""
        try:
            fvalue = float(value)

            if conversion_type == "passthrough":
                return int(fvalue)

            elif conversion_type == "int16_to_float32":
                fvalue = (fvalue - offset) / scale
                if fvalue < 0:
                    fvalue += 65536
                return int(fvalue) & 0xFFFF

            elif conversion_type == "uint16_to_float32":
                return int((fvalue - offset) / scale)

            elif conversion_type == "scale":
                return int((fvalue - offset) / scale)

            elif conversion_type == "percent_to_0_27648":
                return int(fvalue / 100.0 * 27648.0)

            else:
                return int((fvalue - offset) / scale)

        except (ValueError, TypeError, ZeroDivisionError):
            return 0


class ProtocolBridgeManager:
    """协议桥接管理器 - 管理多个协议桥接"""

    def __init__(self):
        self._running = False
        self._bridges: dict[str, ProtocolBridge] = {}
        self._source_data: dict[str, dict[str, Any]] = {}  # device_id -> {point -> value}
        self._conversion = ProtocolConverter()
        self._task: asyncio.Task | None = None
        self._transform_callbacks: list = []

    async def start(self) -> None:
        """启动桥接管理器"""
        self._running = True
        self._task = asyncio.create_task(self._sync_loop())
        logger.info("协议桥接管理器启动，共 %d 个桥接", len(self._bridges))

    async def stop(self) -> None:
        """停止桥接管理器"""
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        logger.info("协议桥接管理器停止")

    def add_bridge(self, bridge: ProtocolBridge) -> None:
        """添加协议桥接"""
        self._bridges[bridge.bridge_id] = bridge
        logger.info("添加协议桥接: %s (%s → %s)",
                   bridge.bridge_id, bridge.source_protocol, bridge.target_protocol)

    def remove_bridge(self, bridge_id: str) -> None:
        """移除协议桥接"""
        self._bridges.pop(bridge_id, None)
        logger.info("移除协议桥接: %s", bridge_id)

    def add_mapping_rule(self, bridge_id: str, rule: MappingRule) -> None:
        """添加映射规则"""
        bridge = self._bridges.get(bridge_id)
        if bridge:
            bridge.mapping_rules.append(rule)
            logger.info("添加映射规则: %s.%s → %s.%s",
                       rule.source_device, rule.source_point,
                       rule.target_device, rule.target_point)

    def remove_mapping_rule(self, bridge_id: str, rule_id: str) -> None:
        """移除映射规则"""
        bridge = self._bridges.get(bridge_id)
        if bridge:
            bridge.mapping_rules = [r for r in bridge.mapping_rules if r.rule_id != rule_id]

    async def update_source_data(self, device_id: str, point: str, value: Any, quality: str = "good") -> None:
        """更新源数据，自动触发转换"""
        if device_id not in self._source_data:
            self._source_data[device_id] = {}
        self._source_data[device_id][point] = {"value": value, "quality": quality, "timestamp": asyncio.get_event_loop().time()}

        # 触发所有相关桥接的转换
        for bridge in self._bridges.values():
            if not bridge.enabled:
                continue
            await self._process_bridge(bridge, device_id, point, value)

    async def _process_bridge(self, bridge: ProtocolBridge, source_device: str, source_point: str, value: Any) -> None:
        """处理单个桥接的转换"""
        # 查找匹配的映射规则
        for rule in bridge.mapping_rules:
            if not rule.enabled:
                continue
            if rule.source_device != source_device or rule.source_point != source_point:
                continue

            try:
                # 数据转换
                converted_value = self._conversion.convert(
                    value, rule.data_type, rule.scale, rule.offset
                )

                # 记录转换结果
                result = {
                    "bridge_id": bridge.bridge_id,
                    "rule_id": rule.rule_id,
                    "source_device": source_device,
                    "source_point": source_point,
                    "target_device": rule.target_device,
                    "target_point": rule.target_point,
                    "original_value": value,
                    "converted_value": converted_value,
                    "quality": "good",
                }

                # 触发转换回调
                for callback in self._transform_callbacks:
                    try:
                        if asyncio.iscoroutine_function(callback):
                            await callback(result)
                        else:
                            callback(result)
                    except Exception as e:
                        logger.warning("转换回调执行失败: %s", e)

                logger.debug("协议转换: %s.%s=%s → %s.%s=%s",
                           source_device, source_point, value,
                           rule.target_device, rule.target_point, converted_value)

            except Exception as e:
                logger.error("协议转换失败: %s - %s", rule.rule_id, e)

    async def _sync_loop(self) -> None:
        """同步循环 - 定时同步数据"""
        while self._running:
            try:
                await asyncio.sleep(1.0)  # 1秒同步周期

                for bridge in self._bridges.values():
                    if not bridge.enabled:
                        continue

                    # 对每个启用的映射规则
                    for rule in bridge.mapping_rules:
                        if not rule.enabled:
                            continue

                        source_data = self._source_data.get(rule.source_device, {})
                        source_value = source_data.get(rule.source_point)

                        if source_value is not None:
                            value = source_value.get("value") if isinstance(source_value, dict) else source_value
                            await self._process_bridge(bridge, rule.source_device, rule.source_point, value)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("协议桥接同步循环异常: %s", e)

    def register_transform_callback(self, callback) -> None:
        """注册转换回调"""
        self._transform_callbacks.append(callback)

    def get_bridges(self) -> list[dict]:
        """获取所有桥接信息"""
        return [
            {
                "bridge_id": b.bridge_id,
                "source_protocol": b.source_protocol,
                "target_protocol": b.target_protocol,
                "enabled": b.enabled,
                "rules_count": len(b.mapping_rules),
            }
            for b in self._bridges.values()
        ]

    def get_bridge_stats(self, bridge_id: str) -> dict | None:
        """获取桥接统计"""
        bridge = self._bridges.get(bridge_id)
        if not bridge:
            return None

        return {
            "bridge_id": bridge.bridge_id,
            "source_protocol": bridge.source_protocol,
            "target_protocol": bridge.target_protocol,
            "enabled": bridge.enabled,
            "rules_count": len(bridge.mapping_rules),
            "enabled_rules": sum(1 for r in bridge.mapping_rules if r.enabled),
        }


class ModbusToOpcUaConverter:
    """Modbus TCP → OPC UA 转换器

    将Modbus寄存器映射为OPC UA节点，
    使OPC UA客户端可以直接访问Modbus设备数据。
    """

    def __init__(self, opcua_server_url: str = "opc.tcp://localhost:4840"):
        self._server_url = opcua_server_url
        self._node_map: dict[str, dict] = {}  # node_id -> {address, data_type, value}
        self._modbus_data: dict[str, Any] = {}

    async def add_mapping(
        self,
        node_id: str,
        modbus_address: int,
        register_count: int = 1,
        data_type: str = "uint16",
        swap_bytes: bool = False,
    ) -> None:
        """添加Modbus地址到OPC UA节点的映射

        Args:
            node_id: OPC UA节点ID
            modbus_address: Modbus寄存器地址
            register_count: 寄存器数量 (1=int16, 2=float32)
            data_type: 数据类型 (uint16/int16/float32)
            swap_bytes: 是否交换字节序
        """
        self._node_map[node_id] = {
            "address": modbus_address,
            "register_count": register_count,
            "data_type": data_type,
            "swap_bytes": swap_bytes,
        }
        logger.info("添加映射: OPC UA %s ← Modbus %d (%s)",
                   node_id, modbus_address, data_type)

    async def update_modbus_data(self, address: int, registers: list[int]) -> None:
        """更新Modbus数据"""
        self._modbus_data[address] = registers

    async def read_node(self, node_id: str) -> Any | None:
        """读取OPC UA节点值 (内部从Modbus数据转换)"""
        mapping = self._node_map.get(node_id)
        if not mapping:
            return None

        address = mapping["address"]
        registers = self._modbus_data.get(address, [])

        if not registers:
            return None

        data_type = mapping.get("data_type", "uint16")
        swap = mapping.get("swap_bytes", False)

        if data_type == "uint16":
            return registers[0] if len(registers) >= 1 else 0

        elif data_type == "int16":
            val = registers[0] if len(registers) >= 1 else 0
            if val >= 32768:
                val -= 65536
            return val

        elif data_type == "float32":
            if len(registers) >= 2:
                high = registers[0]
                low = registers[1]
                if swap:
                    high, low = low, high
                raw = (high << 16) | low
                # IEEE 754 float
                import struct
                return struct.unpack(">f", struct.pack(">I", raw))[0]
            return 0.0

        return registers[0] if registers else 0

    def get_all_nodes(self) -> dict[str, Any]:
        """获取所有节点当前值"""
        result = {}
        for node_id in self._node_map:
            result[node_id] = asyncio.create_task(self.read_node(node_id))
        return result


# 全局实例
_bridge_manager: ProtocolBridgeManager | None = None


def get_bridge_manager() -> ProtocolBridgeManager:
    """获取协议桥接管理器全局实例"""
    global _bridge_manager
    if _bridge_manager is None:
        _bridge_manager = ProtocolBridgeManager()
    return _bridge_manager


import contextlib
