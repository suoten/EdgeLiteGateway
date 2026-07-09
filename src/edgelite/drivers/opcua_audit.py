"""OPC UA 审计日志模块

提供 OPC UA 相关关键操作的审计日志记录能力，包括证书切换、故障切换、
RBAC 权限校验、配置版本管理、OTA 升级等操作的审计追踪。
基于内存 deque(maxlen=10000) 存储，支持按设备/动作过滤、CSV 导出与统计。
可选注入外部 audit_service 进行持久化或上报。
"""

from __future__ import annotations

import csv
import io
import json
import logging
import time
from collections import deque
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


class OpcUaAuditAction(StrEnum):
    """OPC UA 审计动作枚举

    继承 str 便于直接序列化与比较。
    """

    CERT_SWITCH = "cert_switch"
    CERT_REVERT = "cert_revert"
    CONFIG_VERSION_SAVE = "config_version_save"
    CONFIG_VERSION_ROLLBACK = "config_version_rollback"
    OTA_START = "ota_start"
    OTA_START_FAILED = "ota_start_failed"
    OTA_ROLLBACK = "ota_rollback"
    OTA_ROLLBACK_FAILED = "ota_rollback_failed"


class OpcUaAudit:
    """OPC UA 审计日志记录器

    基于内存 deque(maxlen=10000) 存储审计记录，支持按设备/动作过滤、
    CSV 导出与统计。可选注入外部 audit_service 进行持久化或上报。

    所有 log_* 方法均为 async（部分通过 asyncio.create_task 调度，部分直接 await）。
    查询/导出/统计方法为同步方法，直接操作内存数据。

    Args:
        audit_service: 可选的外部审计服务，若提供则记录同时转发给该服务
    """

    _MAX_RECORDS = 10000

    def __init__(self, audit_service: Any = None) -> None:
        self._audit_service = audit_service
        self._records: deque[dict] = deque(maxlen=self._MAX_RECORDS)

    @staticmethod
    def _now_iso() -> str:
        """当前 UTC 时间的 ISO 格式字符串"""
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _action_value(action: Any) -> str:
        """将动作枚举或字符串统一转为字符串"""
        if isinstance(action, OpcUaAuditAction):
            return action.value
        return str(action)

    async def log(self, action: str, device_id: str = "", **kwargs: Any) -> None:
        """通用审计日志记录

        Args:
            action: 审计动作（字符串或 OpcUaAuditAction）
            device_id: 关联设备 ID
            **kwargs: 附加详情字段，存入 details
        """
        record: dict = {
            "timestamp": self._now_iso(),
            "epoch": time.time(),
            "device_id": device_id,
            "action": self._action_value(action),
            "details": dict(kwargs),
        }
        self._records.append(record)
        # 若注入了外部审计服务，尝试转发（容错处理，不影响本地记录）
        if self._audit_service is not None:
            try:
                log_fn = getattr(self._audit_service, "log", None)
                if log_fn is not None:
                    result = log_fn(record)
                    # 兼容 async 与 sync 的外部服务
                    if hasattr(result, "__await__"):
                        await result
            except Exception as e:
                logger.warning("Failed to forward audit log to external service: %s", e)

    async def log_cert_switch(self, device_id: str, action: OpcUaAuditAction) -> None:
        """记录证书切换/恢复操作

        Args:
            device_id: 设备 ID
            action: OpcUaAuditAction.CERT_SWITCH 或 CERT_REVERT
        """
        await self.log(action.value, device_id=device_id, action_type="cert")

    async def log_failover(self, device_id: str, primary: str, backup: str) -> None:
        """记录故障切换操作

        Args:
            device_id: 设备 ID
            primary: 主端地址
            backup: 备端地址
        """
        await self.log("failover", device_id=device_id, primary=primary, backup=backup)

    async def log_rbac_check(
        self, device_id: str, permission: str, role: str, granted: bool
    ) -> None:
        """记录 RBAC 权限校验

        Args:
            device_id: 设备 ID
            permission: 请求的权限名
            role: 请求者角色
            granted: 是否授权通过
        """
        await self.log(
            "rbac_check",
            device_id=device_id,
            permission=permission,
            role=role,
            granted=bool(granted),
        )

    async def log_config_version(
        self,
        device_id: str,
        action: OpcUaAuditAction,
        *,
        to_version: int | None = None,
        from_version: int | None = None,
        operator: str | None = None,
    ) -> None:
        """记录配置版本保存/回滚操作

        Args:
            device_id: 设备 ID
            action: CONFIG_VERSION_SAVE 或 CONFIG_VERSION_ROLLBACK
            to_version: 目标版本号（keyword-only）
            from_version: 源版本号（keyword-only）
            operator: 操作者（keyword-only）
        """
        details: dict = {}
        if to_version is not None:
            details["to_version"] = to_version
        if from_version is not None:
            details["from_version"] = from_version
        if operator is not None:
            details["operator"] = operator
        await self.log(action.value, device_id=device_id, **details)

    async def log_ota(
        self, device_id: str, action: OpcUaAuditAction, version: str | None = None
    ) -> None:
        """记录 OTA 升级/回滚操作

        Args:
            device_id: 设备 ID
            action: OTA_START / OTA_START_FAILED / OTA_ROLLBACK / OTA_ROLLBACK_FAILED
            version: 固件版本（可选）
        """
        details: dict = {}
        if version is not None:
            details["version"] = version
        await self.log(action.value, device_id=device_id, **details)

    def get_recent(self, limit: int) -> list[dict]:
        """获取最近的审计记录（按时间倒序，最新的在前）"""
        if limit <= 0:
            return []
        records = list(self._records)
        return records[-limit:][::-1]

    def get_by_device(self, device_id: str, limit: int) -> list[dict]:
        """按设备 ID 过滤审计记录（倒序）

        device_id 为空的记录视为全局事件（如 OTA），一并包含在结果中。
        """
        if limit <= 0:
            return []
        matched = [
            r for r in self._records
            if r.get("device_id") == device_id or r.get("device_id") == ""
        ]
        return matched[-limit:][::-1]

    def get_by_action(self, action: str | OpcUaAuditAction, limit: int) -> list[dict]:
        """按动作类型过滤审计记录（倒序）"""
        if limit <= 0:
            return []
        action_val = self._action_value(action)
        matched = [r for r in self._records if r.get("action") == action_val]
        return matched[-limit:][::-1]

    def export_csv(self, start_time: str | None = None, end_time: str | None = None) -> str:
        """导出审计记录为 CSV 字符串

        Args:
            start_time: 可选起始时间（ISO 字符串），按 timestamp 字段字符串比较
            end_time: 可选结束时间（ISO 字符串）

        Returns:
            CSV 格式字符串，含表头 timestamp,device_id,action,details
        """
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["timestamp", "device_id", "action", "details"])
        for r in self._records:
            ts = r.get("timestamp", "")
            # ISO 字符串可直接字典序比较
            if start_time is not None and ts < start_time:
                continue
            if end_time is not None and ts > end_time:
                continue
            details = r.get("details", {})
            try:
                details_str = json.dumps(details, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                details_str = str(details)
            writer.writerow([
                ts,
                r.get("device_id", ""),
                r.get("action", ""),
                details_str,
            ])
        return output.getvalue()

    def get_stats(self) -> dict:
        """获取审计统计信息"""
        total = len(self._records)
        by_action: dict = {}
        by_device: dict = {}
        for r in self._records:
            action = r.get("action", "")
            by_action[action] = by_action.get(action, 0) + 1
            device = r.get("device_id", "")
            by_device[device] = by_device.get(device, 0) + 1
        return {
            "total": total,
            "capacity": self._MAX_RECORDS,
            "by_action": by_action,
            "by_device": by_device,
        }


__all__ = ["OpcUaAudit", "OpcUaAuditAction"]
