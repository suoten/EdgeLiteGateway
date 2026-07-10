"""Modbus 操作审计模块 — 记录写操作、配置变更、故障转移和重连事件。

提供内存审计日志，支持按设备/动作/时间范围查询和 CSV 导出。
线程安全: 使用 threading.Lock 保护内部日志列表。
"""

from __future__ import annotations

import csv
import io
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


class ModbusAuditAction(StrEnum):
    """审计动作类型枚举"""

    WRITE = "write"
    CONFIG_CHANGE = "config_change"
    FAILOVER = "failover"
    RECONNECT = "reconnect"
    READ = "read"


@dataclass
class AuditRecord:
    """单条审计记录"""

    timestamp: str
    device_id: str
    action: str
    details: dict[str, Any] = field(default_factory=dict)


class ModbusAudit:
    """Modbus 操作审计日志 — 内存存储，支持查询和导出

    最大容量 10000 条 (FIFO 淘汰)。
    """

    _MAX_RECORDS = 10000

    def __init__(self) -> None:
        self._records: list[AuditRecord] = []
        self._lock = threading.Lock()

    def _add(self, device_id: str, action: ModbusAuditAction, details: dict[str, Any]) -> None:
        record = AuditRecord(
            timestamp=datetime.now(UTC).isoformat(),
            device_id=device_id,
            action=action.value,
            details=details,
        )
        with self._lock:
            self._records.append(record)
            if len(self._records) > self._MAX_RECORDS:
                self._records = self._records[-self._MAX_RECORDS :]

    async def log_write(self, device_id: str, point: str, value: Any, operator: str = "", status: str = "") -> None:
        """记录写操作"""
        self._add(
            device_id,
            ModbusAuditAction.WRITE,
            {
                "point": point,
                "value": value,
                "operator": operator,
                "status": status,
            },
        )

    async def log_config_change(self, device_id: str, keys: list[str], old_config: dict, new_config: dict) -> None:
        """记录配置变更"""
        self._add(
            device_id,
            ModbusAuditAction.CONFIG_CHANGE,
            {
                "keys": keys,
                "old": old_config,
                "new": new_config,
            },
        )

    async def log_failover(self, device_id: str, from_host: str, to_host: str) -> None:
        """记录故障转移"""
        self._add(
            device_id,
            ModbusAuditAction.FAILOVER,
            {
                "from": from_host,
                "to": to_host,
            },
        )

    async def log_reconnect(self, device_id: str, success: bool = True) -> None:
        """记录重连事件"""
        self._add(device_id, ModbusAuditAction.RECONNECT, {"success": success})

    def get_recent(self, limit: int = 100) -> list[dict[str, Any]]:
        """获取最近的审计记录"""
        with self._lock:
            records = self._records[-limit:]
        return [self._to_dict(r) for r in records]

    def get_by_device(self, device_id: str, limit: int = 100) -> list[dict[str, Any]]:
        """按设备 ID 查询审计记录"""
        with self._lock:
            records = [r for r in self._records if r.device_id == device_id][-limit:]
        return [self._to_dict(r) for r in records]

    def get_by_action(self, action: ModbusAuditAction | str, limit: int = 100) -> list[dict[str, Any]]:
        """按动作类型查询审计记录"""
        action_value = action.value if isinstance(action, ModbusAuditAction) else str(action)
        with self._lock:
            records = [r for r in self._records if r.action == action_value][-limit:]
        return [self._to_dict(r) for r in records]

    def get_stats(self) -> dict[str, Any]:
        """获取审计统计摘要"""
        with self._lock:
            total = len(self._records)
            by_action: dict[str, int] = {}
            for r in self._records:
                by_action[r.action] = by_action.get(r.action, 0) + 1
        return {"total": total, "by_action": by_action}

    def export_csv(self, start_time: str | None = None, end_time: str | None = None) -> str:
        """导出审计记录为 CSV 字符串"""
        with self._lock:
            records = list(self._records)
        if start_time:
            records = [r for r in records if r.timestamp >= start_time]
        if end_time:
            records = [r for r in records if r.timestamp <= end_time]
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["timestamp", "device_id", "action", "details"])
        for r in records:
            writer.writerow([r.timestamp, r.device_id, r.action, str(r.details)])
        return output.getvalue()

    @staticmethod
    def _to_dict(record: AuditRecord) -> dict[str, Any]:
        return {
            "timestamp": record.timestamp,
            "device_id": record.device_id,
            "action": record.action,
            "details": record.details,
        }
