"""FINS 驱动审计日志 - 内存环形缓冲实现

为欧姆龙 FINS 协议驱动提供操作审计能力，覆盖：
- 主备链路切换（failover）
- RBAC 权限校验
- 配置版本管理（save/rollback）
- OTA 固件升级生命周期

关键差异（相对 OPC UA/S7）：
- 所有方法均为同步（sync），无 async 方法
- 不使用枚举（FinsAuditAction），action 直接以字符串表达
- log_ota 接收 4 个位置参数（scope/action/version/role）
- export_csv 必传 2 个时间参数，无默认值
"""

from __future__ import annotations

import csv
import io
import logging
import time
from collections import deque
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# 审计记录环形缓冲上限，超出后自动丢弃最旧记录
_MAX_AUDIT_RECORDS = 10000


class FinsAudit:
    """FINS 驱动审计日志器（纯同步实现）。

    所有审计记录保存在内存 deque(maxlen=10000) 中，进程重启后丢失。
    若构造时传入 audit_service（具备 log 等方法的服务对象），
    会同时尝试将记录转发给该服务做持久化，但转发失败不影响本地记录。
    """

    def __init__(self, audit_service: Any = None) -> None:
        # 审计服务（可选），若提供则同步转发记录
        self._audit_service = audit_service
        # 内存环形缓冲，保存审计记录字典
        self._records: deque[dict[str, Any]] = deque(maxlen=_MAX_AUDIT_RECORDS)
        # 简易计数器，用于统计各类 action 发生次数
        self._action_counts: dict[str, int] = {}

    def _append(self, device_id: str, action: str, details: dict[str, Any]) -> None:
        """追加一条审计记录到内存缓冲，并更新计数。"""
        record: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "epoch": time.time(),
            "device_id": device_id,
            "action": action,
            "details": details,
        }
        self._records.append(record)
        self._action_counts[action] = self._action_counts.get(action, 0) + 1
        # 转发给外部审计服务（best-effort，失败不抛出）
        if self._audit_service is not None:
            try:
                log_fn = getattr(self._audit_service, "log", None)
                if callable(log_fn):
                    log_fn(record)
            except Exception as e:  # noqa: BLE001 - 审计转发失败不应中断主流程
                logger.debug("[fins-audit] 转发到 audit_service 失败: %s", e)

    def log_failover(self, device_id: str, primary_ip: str, backup_ip: str) -> None:
        """记录主备链路切换事件。"""
        self._append(
            device_id,
            "failover",
            {"primary_ip": primary_ip, "backup_ip": backup_ip},
        )

    def log_rbac_check(
        self,
        device_id: str,
        permission: str,
        role: str,
        granted: bool,
    ) -> None:
        """记录 RBAC 权限校验结果。"""
        self._append(
            device_id,
            "rbac_check",
            {
                "permission": permission,
                "role": role,
                "granted": bool(granted),
            },
        )

    def log_config_version(
        self,
        device_id: str,
        action: str,
        *,
        to_version: int | None = None,
        from_version: int | None = None,
        operator: str | None = None,
    ) -> None:
        """记录配置版本管理操作（action 如 save/rollback）。"""
        self._append(
            device_id,
            f"config_{action}",
            {
                "to_version": to_version,
                "from_version": from_version,
                "operator": operator,
            },
        )

    def log_ota(
        self,
        scope: str,
        action: str,
        version: str,
        role: str,
    ) -> None:
        """记录 OTA 固件升级生命周期事件。

        Args:
            scope: 作用域，例如 global
            action: 动作名称，例如 ota_start / ota_completed
            version: 目标固件版本字符串
            role: 发起操作的用户角色
        """
        self._append(
            scope,
            action,
            {"version": version, "role": role},
        )

    def get_recent(self, limit: int) -> list[dict[str, Any]]:
        """获取最近的审计记录（按时间倒序，最多 limit 条）。"""
        if limit <= 0:
            return []
        records = list(self._records)
        records.reverse()
        return records[:limit]

    def get_by_device(self, device_id: str, limit: int) -> list[dict[str, Any]]:
        """按设备 ID 过滤审计记录（倒序，最多 limit 条）。

        全局作用域（device_id='global'）的记录（如 OTA 事件）同时纳入
        每台设备的审计视图，因为全局事件同样影响具体设备。
        """
        if limit <= 0:
            return []
        results: list[dict[str, Any]] = []
        for record in reversed(self._records):
            rec_dev = record.get("device_id")
            if rec_dev == device_id or rec_dev == "global":
                results.append(record)
                if len(results) >= limit:
                    break
        return results

    def get_by_action(self, action: str, limit: int) -> list[dict[str, Any]]:
        """按 action 过滤审计记录（倒序，最多 limit 条）。"""
        if limit <= 0:
            return []
        results: list[dict[str, Any]] = []
        for record in reversed(self._records):
            if record.get("action") == action:
                results.append(record)
                if len(results) >= limit:
                    break
        return results

    def export_csv(self, start_time: str, end_time: str) -> str:
        """导出指定时间范围内的审计记录为 CSV 字符串。

        时间范围过滤基于记录 timestamp 字符串比较，兼容 ISO 与 YYYY-MM-DD。
        """
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["timestamp", "device_id", "action", "details"])
        for record in self._records:
            ts = record.get("timestamp", "")
            if start_time and ts < start_time:
                continue
            if end_time and ts > end_time:
                continue
            writer.writerow(
                [
                    ts,
                    record.get("device_id", ""),
                    record.get("action", ""),
                    record.get("details", ""),
                ]
            )
        return output.getvalue()

    def get_stats(self) -> dict[str, Any]:
        """获取审计统计信息。"""
        return {
            "total_records": len(self._records),
            "max_capacity": _MAX_AUDIT_RECORDS,
            "action_counts": dict(self._action_counts),
        }
