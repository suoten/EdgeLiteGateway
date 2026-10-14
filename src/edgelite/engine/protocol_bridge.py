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
import contextlib  # FIXED-P2: 原import在文件末尾(行463)，移至顶部与其他标准库import一起，避免维护混乱
import json
import logging
import struct
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
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
    def convert(value: Any, conversion_type: str, scale: float = 1.0, offset: float = 0.0) -> float | int | bool | None:
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

            elif conversion_type == "uint16_to_float32" or conversion_type == "scale":
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
            # FIXED-P1: 转换失败返回None而非0.0，避免与有效传感器读数0.0混淆
            return None

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

            elif conversion_type == "uint16_to_float32" or conversion_type == "scale":
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
        # FIXED-P2: _bridges 变更保护——API 的 update/enable/disable 与 _sync_loop 后台读
        # 并发修改同一 dataclass 属性会产生撕裂读；变更走锁，读走快照
        self._bridge_lock = threading.Lock()
        # FIXED-P2: 桥接配置持久化路径——原实现纯内存态，网关重启后全部桥接丢失
        self._persist_path = Path("data/protocol_bridges.json")
        self._source_data: dict[str, dict[str, Any]] = {}  # device_id -> {point -> value}
        self._conversion = ProtocolConverter()
        self._task: asyncio.Task | None = None
        self._transform_callbacks: list = []
        # FIXED-P2: _processed_versions提升为实例变量，update_source_data与_sync_loop共享，避免重复处理同一数据点
        self._processed_versions: dict[str, float] = {}

    async def start(self) -> None:
        """启动桥接管理器"""
        self._running = True
        loaded = self.load_config()
        if loaded:
            logger.info("协议桥接配置从磁盘恢复 %d 个桥接", loaded)
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

    # ── 持久化（FIXED-P2: 重启恢复） ──

    def save_config(self) -> None:
        """桥接配置原子落盘（tmp + replace）。API 每次变更后调用；失败仅告警不阻断。"""
        try:
            with self._bridge_lock:
                payload = {
                    "version": 1,
                    "saved_at": datetime.now(UTC).isoformat(),
                    "bridges": [asdict(b) for b in self._bridges.values()],
                }
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._persist_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._persist_path)
        except Exception as e:
            logger.error("协议桥接配置持久化失败: %s", e)

    def load_config(self) -> int:
        """启动时从磁盘恢复桥接配置，返回加载数量；文件缺失/损坏返回 0（不抛异常）。"""
        try:
            if not self._persist_path.exists():
                return 0
            data = json.loads(self._persist_path.read_text(encoding="utf-8"))
            bridges_raw = data.get("bridges", []) if isinstance(data, dict) else []
            loaded = 0
            with self._bridge_lock:
                for item in bridges_raw:
                    try:
                        if not isinstance(item, dict) or not item.get("bridge_id"):
                            continue
                        rules = [MappingRule(**r) for r in item.get("mapping_rules", []) if isinstance(r, dict)]
                        bridge = ProtocolBridge(
                            bridge_id=str(item["bridge_id"]),
                            source_protocol=str(item.get("source_protocol", "")),
                            target_protocol=str(item.get("target_protocol", "")),
                            source_config=item.get("source_config", {}) or {},
                            target_config=item.get("target_config", {}) or {},
                            mapping_rules=rules,
                            enabled=bool(item.get("enabled", True)),
                        )
                        self._bridges[bridge.bridge_id] = bridge
                        loaded += 1
                    except Exception as e:
                        logger.warning("跳过损坏的桥接配置条目: %s", e)
            return loaded
        except FileNotFoundError:
            return 0
        except Exception as e:
            logger.error("协议桥接配置加载失败: %s", e)
            return 0

    # ── 配置变更（加锁 + 落盘） ──

    def add_bridge(self, bridge: ProtocolBridge) -> None:
        """添加协议桥接"""
        with self._bridge_lock:
            self._bridges[bridge.bridge_id] = bridge
        logger.info("添加协议桥接: %s (%s → %s)", bridge.bridge_id, bridge.source_protocol, bridge.target_protocol)

    def remove_bridge(self, bridge_id: str) -> None:
        """移除协议桥接"""
        with self._bridge_lock:
            self._bridges.pop(bridge_id, None)
        logger.info("移除协议桥接: %s", bridge_id)

    def get_bridges_snapshot(self) -> dict[str, ProtocolBridge]:
        """获取 _bridges 快照（浅拷贝 dict；ProtocolBridge 实例共享，仅读场景使用）"""
        with self._bridge_lock:
            return dict(self._bridges)

    def get_bridge_by_id(self, bridge_id: str) -> ProtocolBridge | None:
        """按 ID 获取桥接实例（读锁快照）"""
        with self._bridge_lock:
            return self._bridges.get(bridge_id)

    def update_bridge_fields(
        self,
        bridge_id: str,
        *,
        source_protocol: str | None = None,
        target_protocol: str | None = None,
        enabled: bool | None = None,
        mapping_rules: list[MappingRule] | None = None,
        source_config: dict | None = None,
    ) -> ProtocolBridge | None:
        """在锁内更新桥接字段（避免 API 直接改共享 dataclass 与 _sync_loop 竞态）"""
        with self._bridge_lock:
            bridge = self._bridges.get(bridge_id)
            if bridge is None:
                return None
            if source_protocol is not None:
                bridge.source_protocol = source_protocol
            if target_protocol is not None:
                bridge.target_protocol = target_protocol
            if enabled is not None:
                bridge.enabled = enabled
            if mapping_rules is not None:
                bridge.mapping_rules = mapping_rules
            if source_config is not None:
                bridge.source_config = source_config
            return bridge

    def add_mapping_rule(self, bridge_id: str, rule: MappingRule) -> None:
        """添加映射规则"""
        with self._bridge_lock:
            bridge = self._bridges.get(bridge_id)
            if bridge:
                bridge.mapping_rules.append(rule)
        if bridge:
            logger.info(
                "添加映射规则: %s.%s → %s.%s",
                rule.source_device,
                rule.source_point,
                rule.target_device,
                rule.target_point,
            )

    def remove_mapping_rule(self, bridge_id: str, rule_id: str) -> None:
        """移除映射规则"""
        with self._bridge_lock:
            bridge = self._bridges.get(bridge_id)
            if bridge:
                bridge.mapping_rules = [r for r in bridge.mapping_rules if r.rule_id != rule_id]

    async def update_source_data(self, device_id: str, point: str, value: Any, quality: str = "good") -> None:
        """更新源数据，自动触发转换"""
        if device_id not in self._source_data:
            self._source_data[device_id] = {}
        # FIXED-P3: 原问题-使用已弃用的asyncio.get_event_loop().time()；改为time.monotonic()
        ts = time.monotonic()
        self._source_data[device_id][point] = {"value": value, "quality": quality, "timestamp": ts}

        # FIXED-P2: 更新已处理版本，避免_sync_loop重复处理同一数据点
        version_key = f"{device_id}:{point}"
        self._processed_versions[version_key] = ts

        # 触发所有相关桥接的转换
        # R11-ENG-05: 迭代 dict 前取快照，避免 await 期间 dict 被修改导致 RuntimeError
        # FIXED-P2: 快照经 _bridge_lock 获取，与 API 变更互斥
        for bridge in self.get_bridges_snapshot().values():
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
                converted_value = self._conversion.convert(value, rule.data_type, rule.scale, rule.offset)

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
                    "quality": "good" if converted_value is not None else "bad",  # FIXED-P2: 转换失败标记quality=bad
                }

                # 触发转换回调
                for callback in self._transform_callbacks:
                    try:
                        if asyncio.iscoroutinefunction(callback):
                            await callback(result)
                        else:
                            callback(result)
                    except Exception as e:
                        logger.warning("转换回调执行失败: %s", e)

                logger.debug(
                    "协议转换: %s.%s=%s → %s.%s=%s",
                    source_device,
                    source_point,
                    value,
                    rule.target_device,
                    rule.target_point,
                    converted_value,
                )

            except Exception as e:
                logger.error("协议转换失败: %s - %s", rule.rule_id, e)

    async def _sync_loop(self) -> None:
        """同步循环 - 定时同步数据"""
        # FIXED-P2: _processed_versions提升为实例变量，与update_source_data共享
        last_cleanup = time.monotonic()
        while self._running:
            try:
                await asyncio.sleep(1.0)

                # FIXED-P3: 定期清理过期版本（10分钟未更新），防止_processed_versions无限增长
                now = time.monotonic()
                if now - last_cleanup >= 600.0:
                    expired = [k for k, v in self._processed_versions.items() if now - v > 600.0]
                    for k in expired:
                        self._processed_versions.pop(k, None)
                    if expired:
                        logger.debug("协议桥接清理%d个过期版本记录", len(expired))
                    last_cleanup = now

                # R11-ENG-05: 迭代 dict 前取快照，避免 await 期间 dict 被修改导致 RuntimeError
                for bridge in self.get_bridges_snapshot().values():
                    if not bridge.enabled:
                        continue

                    for rule in bridge.mapping_rules:
                        if not rule.enabled:
                            continue

                        source_data = self._source_data.get(rule.source_device, {})
                        source_value = source_data.get(rule.source_point)

                        if source_value is not None:
                            value = source_value.get("value") if isinstance(source_value, dict) else source_value
                            ts = source_value.get("timestamp", 0) if isinstance(source_value, dict) else 0
                            version_key = f"{rule.source_device}:{rule.source_point}"
                            if self._processed_versions.get(version_key) == ts:
                                continue
                            self._processed_versions[version_key] = ts
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
            for b in self.get_bridges_snapshot().values()
        ]

    def get_bridge_stats(self, bridge_id: str) -> dict | None:
        """获取桥接统计"""
        bridge = self.get_bridge_by_id(bridge_id)
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
        self._modbus_data: dict[int, Any] = {}

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
        logger.info("添加映射: OPC UA %s ← Modbus %d (%s)", node_id, modbus_address, data_type)

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
        """获取所有节点当前值（同步版本，基于本地 _modbus_data 缓存）

        适用于周期性轮询场景，返回当前缓存值，无需等待异步操作。
        若需获取最新值（包含从 Modbus 设备实时读取），请使用 await_all_nodes()。
        """
        result: dict[str, Any] = {}
        for node_id in self._node_map:
            mapping = self._node_map.get(node_id, {})
            address = mapping.get("address")
            if address is None:
                result[node_id] = None
                continue
            registers = self._modbus_data.get(address, [])
            data_type = mapping.get("data_type", "uint16")
            swap = mapping.get("swap_bytes", False)
            if data_type == "uint16":
                result[node_id] = registers[0] if len(registers) >= 1 else 0
            elif data_type == "int16":
                val = registers[0] if len(registers) >= 1 else 0
                if val >= 32768:
                    val -= 65536
                result[node_id] = val
            elif data_type == "float32":
                if len(registers) >= 2:
                    high, low = registers[0], registers[1]
                    if swap:
                        high, low = low, high
                    raw = (high << 16) | low
                    import struct

                    result[node_id] = struct.unpack(">f", struct.pack(">I", raw))[0]
                else:
                    result[node_id] = 0.0
            else:
                result[node_id] = registers[0] if registers else 0
        return result  # FIXED-P0: 同步方法应返回实际值而非Task对象，原实现asyncio.create_task在同步上下文中无法await，导致调用方拿到coroutine而非数据  # noqa: E501


# 全局实例
_bridge_manager: ProtocolBridgeManager | None = None


def get_bridge_manager() -> ProtocolBridgeManager:
    """获取协议桥接管理器全局实例"""
    global _bridge_manager
    if _bridge_manager is None:
        _bridge_manager = ProtocolBridgeManager()
    return _bridge_manager
