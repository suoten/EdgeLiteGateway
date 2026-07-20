"""Enhanced alarm service with notification, escalation, suppression and statistics

This module provides comprehensive alarm management capabilities including:
- Multi-channel notifications (DingTalk, WeCom, Email, Webhook)
- Alarm escalation based on duration thresholds
- Alarm suppression/masking rules
- Alarm statistics (MTTR, MTBF, trends)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from edgelite.engine.event_bus import AlarmEvent, EventBus
from edgelite.services.notification import (
    AlarmNotification,
    NotificationManager,
    get_notification_manager,
)
from edgelite.storage.sqlite_repo import AlarmRepo

logger = logging.getLogger(__name__)


# Severity levels
SEVERITY_CRITICAL = "critical"
SEVERITY_MAJOR = "major"
SEVERITY_MINOR = "minor"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"

SEVERITY_ORDER = [SEVERITY_INFO, SEVERITY_WARNING, SEVERITY_MINOR, SEVERITY_MAJOR, SEVERITY_CRITICAL]


@dataclass
class AlarmEscalationConfig:
    """Alarm escalation configuration"""

    severity: str
    threshold_seconds: int  # Time before escalation
    escalate_to: str  # Target severity level
    notify_channels: list[str] = field(default_factory=list)


@dataclass
class AlarmSuppressionRule:
    """Alarm suppression rule"""

    rule_id: str
    name: str
    device_ids: list[str] = field(default_factory=list)
    rule_ids: list[str] = field(default_factory=list)
    severities: list[str] = field(default_factory=list)
    time_range_start: str = ""
    time_range_end: str = ""
    enabled: bool = True
    # FIXED-P0: 添加过期时间，防止抑制规则无限累积
    expires_at: datetime | None = None


@dataclass
class AlarmStatistics:
    """Alarm statistics"""

    total_count: int = 0
    firing_count: int = 0
    acknowledged_count: int = 0
    recovered_count: int = 0
    # FIXED-P1: 添加 escalated_count 字段，原代码使用 getattr(self._stats, "escalated_count", 0) 绕过，
    # 导致统计字段缺失，数据不一致
    escalated_count: int = 0
    by_severity: dict[str, int] = field(default_factory=dict)
    # MTTR (Mean Time To Recovery) in seconds
    mttr_seconds: float = 0.0
    # MTBF (Mean Time Between Failures) in seconds
    mtbf_seconds: float = 0.0
    # Recovery times for MTTR calculation
    recovery_times: list[float] = field(default_factory=list)
    # Time between failures for MTBF calculation
    failure_intervals: list[float] = field(default_factory=list)


@dataclass
class AlarmEscalationState:
    """State for alarm escalation tracking"""

    alarm_id: str
    severity: str
    start_time: datetime
    escalation_level: int = 0
    has_escalated: bool = False


class AlarmService:
    """Enhanced alarm service with full notification and statistics support"""

    def __init__(
        self,
        alarm_repo: AlarmRepo,
        event_bus: EventBus | None = None,
        notification_manager: NotificationManager | None = None,
        name_resolver: Any | None = None,
    ):
        self._repo = alarm_repo
        self._event_bus = event_bus
        self._notification_manager = notification_manager or get_notification_manager()
        # FIXED: 名称解析器，用于在告警恢复/确认时查询 rule_name 和 device_name
        # name_resolver 需要提供 async resolve(rule_id, device_id) -> (rule_name, device_name) 方法
        self._name_resolver = name_resolver

        # Escalation configuration
        self._escalation_configs: dict[str, AlarmEscalationConfig] = {
            SEVERITY_CRITICAL: AlarmEscalationConfig(
                severity=SEVERITY_CRITICAL,
                threshold_seconds=300,  # 5 minutes
                escalate_to=SEVERITY_CRITICAL,
            ),
            SEVERITY_MAJOR: AlarmEscalationConfig(
                severity=SEVERITY_MAJOR,
                threshold_seconds=900,  # 15 minutes
                escalate_to=SEVERITY_CRITICAL,
            ),
            SEVERITY_MINOR: AlarmEscalationConfig(
                severity=SEVERITY_MINOR,
                threshold_seconds=1800,  # 30 minutes
                escalate_to=SEVERITY_MAJOR,
            ),
            SEVERITY_WARNING: AlarmEscalationConfig(
                severity=SEVERITY_WARNING,
                threshold_seconds=3600,  # 1 hour
                escalate_to=SEVERITY_MINOR,
            ),
        }
        self._escalation_tasks: dict[str, asyncio.Task] = {}
        # FIXED(严重): 保护 _escalation_tasks 的"取消旧任务+创建新任务"流程，
        # 避免并发调用交错产生孤儿任务导致重复告警升级
        self._escalation_lock = asyncio.Lock()

        # Suppression rules
        self._suppression_rules: list[AlarmSuppressionRule] = []

        # Statistics
        self._stats = AlarmStatistics()
        self._alarm_start_times: dict[str, datetime] = {}
        # FIXED(严重): 引入 _stats_lock 保护 _stats 和 _alarm_start_times 的并发修改
        # 原问题：多个 AlarmEvent handler 可能并发执行，+= 操作非原子（read-modify-write），
        # 并发修改导致计数丢失；_alarm_start_times 的 set/del 跨 await 可能引发 KeyError
        self._stats_lock = asyncio.Lock()
        # FIXED(P2): 原问题-_last_recovery_time计算的是uptime(MTTF)而非MTBF(连续故障间隔);
        # 修复-改用_last_fire_time记录上次告警触发时间，MTBF=当前触发时间-上次触发时间
        self._last_fire_time: datetime | None = None
        # FIXED(P2): 原问题-_calculate_alarm_rate依赖_alarm_start_times(恢复后被清空)，
        # 导致全部恢复后速率返回0; 修复-用_first_alarm_time记录首次告警时间作为统计窗口起点
        self._first_alarm_time: datetime | None = None

        # Task for statistics cleanup
        self._cleanup_task: asyncio.Task | None = None

        # R9-S-02: 记录告警升级前的原始严重度，用于恢复时还原。
        # key=alarm_id, value=升级前的 severity（仅首次升级时保存，保留最原始值）
        self._original_severities: dict[str, str] = {}
        # 并发安全: 专用锁保护 _original_severities 的并发读写
        # _handle_recovery 的 pop 与 _escalate_alarm 的 check+set 可能并发执行，无锁会导致数据不一致
        self._original_severities_lock = asyncio.Lock()

        # R5-F-09 修复(致命): per-alarm-id 互斥锁，防止 handle_alarm_event 的 get→create→notify
        # TOCTOU 竞态导致重复创建告警/重复发送通知。
        # _alarm_locks_meta 保护 _alarm_locks 字典本身的并发创建。
        self._alarm_locks: dict[str, asyncio.Lock] = {}
        self._alarm_locks_meta = asyncio.Lock()
        # 已处理告警去重集合，防止同一 alarm_id 被重复处理（二次到达时直接跳过）
        self._handled_alarm_ids: set[str] = set()
        self._handled_alarm_ids_lock = asyncio.Lock()

    async def _resolve_names(self, rule_id: str, device_id: str) -> tuple[str, str]:
        """解析 rule_name 和 device_name

        FIXED: AlarmORM 不存储 rule_name/device_name，告警恢复/确认时
        需要通过 name_resolver 查询名称，否则前端通知显示"未知规则: 未知设备"

        R9-S-15 修复: 使用 asyncio.gather 并发查询 rule_name 和 device_name，
        避免顺序查询导致的 N+1 性能问题。

        Returns:
            (rule_name, device_name) — 如果查询失败，rule_name fallback 为 rule_id，
            device_name fallback 为 device_id
        """
        rule_name = rule_id  # FIXED: fallback 为 rule_id 而非空字符串，避免前端显示"未知规则"
        device_name = device_id  # FIXED: fallback 为 device_id 而非空字符串，避免前端显示"未知设备"
        if self._name_resolver and hasattr(self._name_resolver, "resolve"):
            try:
                # R9-S-15: 并发查询 rule_name 和 device_name，避免顺序 N+1 查询。
                # 分别传入空字符串使 resolver 仅查询对应名称，通过 gather 并发执行。
                (r_rule, _), (_, r_device) = await asyncio.gather(
                    self._name_resolver.resolve(rule_id, ""),
                    self._name_resolver.resolve("", device_id),
                )
                if r_rule:
                    rule_name = r_rule
                if r_device:
                    device_name = r_device
            except Exception as e:
                logger.debug("Name resolution failed for rule=%s device=%s: %s", rule_id, device_id, e)
        return rule_name, device_name

    async def start(self) -> None:
        """Start the alarm service"""
        # Subscribe to alarm events
        if self._event_bus:
            self._event_bus.register_handler("AlarmEvent", self._handle_alarm_event)
            logger.info("Alarm service started with event bus integration")
        else:
            logger.info("Alarm service started (no event bus integration)")

        # Start cleanup task
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def stop(self) -> None:
        """Stop the alarm service"""
        # Cancel all escalation tasks
        for task in self._escalation_tasks.values():
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._escalation_tasks.clear()

        # Cancel cleanup task
        if self._cleanup_task:
            self._cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._cleanup_task

        logger.info("Alarm service stopped")

    # ============== Notification Integration ==============

    async def _send_notification(
        self,
        alarm: dict,
        action: str,
        channels: list[str] | None = None,
        escalation_level: int = 0,
        original_severity: str = "",
    ) -> dict[str, bool]:
        """Send notification for an alarm event"""
        notification = AlarmNotification(
            alarm_id=alarm.get("alarm_id", ""),
            rule_id=alarm.get("rule_id", ""),
            rule_name=alarm.get("rule_name", "Unknown Rule"),
            device_id=alarm.get("device_id", ""),
            device_name=alarm.get("device_name", alarm.get("device_id", "")),
            severity=alarm.get("severity", SEVERITY_INFO),
            action=action,
            message=alarm.get("message", ""),
            trigger_value=alarm.get("trigger_value", {}),
            trigger_count=alarm.get("trigger_count", 1),
            escalation_level=escalation_level,
            original_severity=original_severity,
        )
        return await self._notification_manager.send_notification(notification, channels)

    # ============== Alarm Event Handling ==============

    async def _handle_alarm_event(self, event: AlarmEvent) -> None:
        """Handle alarm events from the event bus"""
        try:
            if event.action == "firing":
                await self.handle_alarm_event(event)
            elif event.action == "recovered":
                await self._handle_recovery(event)
            elif event.action == "acknowledged":
                await self._handle_acknowledgment(event)
            elif event.action == "escalated":
                # FIXED: 告警升级事件 — 更新统计 + 推送到前端
                await self._handle_escalation(event)
            else:
                logger.debug("Unknown alarm action: %s", event.action)
        except Exception as e:
            logger.error("Error handling alarm event: %s", e)

    async def _get_alarm_lock(self, alarm_id: str) -> asyncio.Lock:
        """R5-F-09: 获取 per-alarm-id 互斥锁，保护 get→create→notify 原子性。"""
        async with self._alarm_locks_meta:
            lock = self._alarm_locks.get(alarm_id)
            if lock is None:
                lock = asyncio.Lock()
                self._alarm_locks[alarm_id] = lock
            return lock

    async def handle_alarm_event(self, event: AlarmEvent) -> None:
        """Handle alarm firing event"""
        alarm_id = event.alarm_id
        rule_id = event.rule_id
        device_id = event.device_id
        severity = event.severity

        logger.info("Alarm fired: %s (rule=%s, device=%s, severity=%s)", alarm_id, rule_id, device_id, severity)

        # R5-F-09 修复(致命): 去重检查 — 同一 alarm_id 仅处理一次，防止事件总线重投
        # 导致重复通知。recover/acknowledge 等其他 action 不在此去重（它们有独立路径）。
        async with self._handled_alarm_ids_lock:
            if alarm_id in self._handled_alarm_ids:
                logger.info("Alarm %s already handled, skipping duplicate event", alarm_id)
                return
            self._handled_alarm_ids.add(alarm_id)

        # Check suppression
        if await self._is_suppressed(rule_id, device_id, severity):
            logger.info("Alarm %s suppressed by rule", alarm_id)
            # R11-SVC-03: suppressed 路径直接 return，需清理已加入的 alarm_id，
            # 否则无 recovery 事件触发 discard，导致 _handled_alarm_ids 逐渐泄漏
            async with self._handled_alarm_ids_lock:
                self._handled_alarm_ids.discard(alarm_id)
            return

        # FIXED-P0: 检查报警静默规则（数据库持久化的静默规则，与内存抑制规则互补）
        # is_silenced是同步方法且内部使用run_coroutine_threadsafe，通过to_thread避免阻塞事件循环
        try:
            from edgelite.services.alarm_silence import get_alarm_silence_manager

            _silence_mgr = get_alarm_silence_manager()
            # FIXED-mypy: is_silenced 需要 silences 参数，先异步获取静默规则列表再传入
            _silences = await _silence_mgr.list_silences(device_id=device_id, rule_id=rule_id)
            _is_silenced = await asyncio.to_thread(_silence_mgr.is_silenced, _silences, device_id, rule_id)
            if _is_silenced:
                logger.info("Alarm %s silenced by silence rule (device=%s, rule=%s)", alarm_id, device_id, rule_id)
                # R11-SVC-03: silenced 路径直接 return，需清理已加入的 alarm_id，
                # 否则无 recovery 事件触发 discard，导致 _handled_alarm_ids 逐渐泄漏
                async with self._handled_alarm_ids_lock:
                    self._handled_alarm_ids.discard(alarm_id)
                return
        except Exception as e:
            logger.debug("Silence check failed for alarm %s: %s", alarm_id, e)

        # R5-F-09 修复(致命): per-alarm-id 锁保护 get→create 原子性，防止并发事件
        # 重复创建告警记录或重复发送通知。锁仅保护 DB 读-改-写，通知等耗时操作在锁外。
        alarm_lock = await self._get_alarm_lock(alarm_id)
        async with alarm_lock:
            # Get alarm details from database
            alarm = await self._repo.get(alarm_id)
            if not alarm:
                # FIXED-BugR11: 边缘规则引擎（快速路径）发布的告警事件未写入数据库，
                # 此处根据事件数据创建告警记录，确保通知/升级/统计正常工作
                logger.info("Alarm %s not in DB, creating from event data", alarm_id)
                alarm_data = {
                    "alarm_id": alarm_id,
                    "rule_id": rule_id,
                    "device_id": device_id,
                    "severity": severity,
                    "message": "",
                    "trigger_value": event.trigger_value,
                    "rule_type": event.rule_type or "threshold",
                }
                try:
                    alarm = await self._repo.create_with_id(alarm_data)
                except Exception as e:
                    # R5-F-09: create_with_id 失败可能是并发已创建成功，重新 get 一次
                    logger.warning("Failed to create alarm from event %s: %s, retrying get", alarm_id, e)
                    alarm = await self._repo.get(alarm_id)
                if not alarm:
                    logger.warning("Failed to create alarm from event: %s", alarm_id)
                    return

        # FIXED-BugR14: 补充 alarm 字典中缺失的 rule_name 和 device_name
        # _orm_to_alarm 不包含这两个字段，通知会显示 "Unknown Rule"
        if not alarm.get("rule_name"):
            alarm["rule_name"] = event.rule_name or rule_id
        if not alarm.get("device_name"):
            alarm["device_name"] = event.device_name or device_id

        # Send notification
        # FIXED-P1: 原问题-_send_notification返回值被丢弃，失败渠道无日志记录
        _notify_results = await self._send_notification(alarm, "firing")
        _failed_channels = [ch for ch, ok in _notify_results.items() if not ok]
        if _failed_channels:
            logger.warning("Alarm %s notification failed on channels: %s", alarm_id, _failed_channels)

        # Schedule escalation if configured
        await self._schedule_escalation(alarm_id, severity, alarm)

        # Update statistics
        # FIXED(严重): 加 _stats_lock 保护并发修改，避免 += 操作非原子导致计数丢失
        fire_time = datetime.now(UTC)
        async with self._stats_lock:
            self._alarm_start_times[alarm_id] = fire_time
            self._stats.firing_count += 1
            self._stats.total_count += 1
            self._stats.by_severity[severity] = self._stats.by_severity.get(severity, 0) + 1
            # FIXED(P2): 原问题-MTBF在_handle_recovery中用(start_time-last_recovery_time)计算，
            # 实际是MTTF(uptime)而非MTBF(连续故障间隔); 修复-在告警触发时用(当前触发-上次触发)计算MTBF
            if self._last_fire_time:
                interval = (fire_time - self._last_fire_time).total_seconds()
                self._stats.failure_intervals.append(interval)
                if len(self._stats.failure_intervals) > 100:
                    self._stats.failure_intervals = self._stats.failure_intervals[-100:]
            self._last_fire_time = fire_time
            # FIXED(P2): 原问题-_calculate_alarm_rate用_alarm_start_times作窗口起点(恢复后清空);
            # 修复-用_first_alarm_time记录首次告警时间，不受恢复影响
            if not self._first_alarm_time:
                self._first_alarm_time = fire_time

    async def _handle_recovery(self, event: AlarmEvent) -> None:
        """Handle alarm recovery event"""
        alarm_id = event.alarm_id

        logger.info("Alarm recovered: %s", alarm_id)

        # Cancel any pending escalation
        await self._cancel_escalation(alarm_id)

        # R9-S-02 修复: 恢复原始严重度（若告警曾被升级）
        # 升级时在 _original_severities 中保存了升级前的 severity，
        # 恢复时需还原为原始值，并清除该字段
        # 并发安全: 加锁保护 _original_severities 的 pop 操作
        async with self._original_severities_lock:
            original_severity = self._original_severities.pop(alarm_id, None)
        if original_severity:
            try:
                await self._repo.update_severity(alarm_id, original_severity)
                logger.info("告警 %s 严重度已恢复为原始值: %s", alarm_id, original_severity)
            except Exception as e:
                logger.error("恢复告警原始严重度失败 %s: %s", alarm_id, e)

        # Calculate duration
        # FIXED(严重): 加 _stats_lock 保护 _alarm_start_times 并发读写
        async with self._stats_lock:
            start_time = self._alarm_start_times.pop(alarm_id, None)
        duration_seconds = 0.0
        if start_time:
            duration_seconds = (datetime.now(UTC) - start_time).total_seconds()

            # Record recovery time for MTTR calculation
            async with self._stats_lock:
                self._recovery_times.append(duration_seconds)
                if len(self._recovery_times) > 100:  # Keep last 100
                    self._recovery_times = self._recovery_times[-100:]
            # FIXED(P2): 原问题-此处用(start_time-_last_recovery_time)计算MTBF，实际是MTTF(uptime);
            # 修复-MTBF计算已移至handle_alarm_event，用连续两次触发时间差计算，此处不再重复

        # Get alarm details
        alarm = await self._repo.get(alarm_id)
        if alarm:
            alarm["duration_seconds"] = duration_seconds
            # FIXED-BugR14: 补充 rule_name/device_name，避免恢复通知显示 "Unknown Rule"
            if not alarm.get("rule_name"):
                alarm["rule_name"] = event.rule_name or event.rule_id
            if not alarm.get("device_name"):
                alarm["device_name"] = event.device_name or event.device_id
            await self._send_notification(alarm, "recovered")

        # Update statistics
        # FIXED(严重): 加 _stats_lock 保护并发修改
        # FIX-EL-FATAL: 原 firingCount/recoveredCount 为 camelCase 误写，AlarmStatistics
        # dataclass 字段为 snake_case(firing_count/recovered_count)。camelCase 属性
        # 不存在导致 AttributeError，事件总线捕获后仅记录日志但不执行后续清理逻辑，
        # 造成 _handled_alarm_ids/_alarm_locks 无限增长(内存泄漏)且统计永远错误。
        async with self._stats_lock:
            self._stats.firing_count = max(0, self._stats.firing_count - 1)
            self._stats.recovered_count += 1
            self._update_mttr_mtbf()

        # R7-S-03 修复(严重): 告警恢复后清理 _handled_alarm_ids 和 _alarm_locks 中的残留条目，
        # 防止这两个集合随告警累积无限增长导致内存泄漏
        async with self._handled_alarm_ids_lock:
            self._handled_alarm_ids.discard(alarm_id)
        async with self._alarm_locks_meta:
            self._alarm_locks.pop(alarm_id, None)

    async def _handle_acknowledgment(self, event: AlarmEvent) -> None:
        """Handle alarm acknowledgment event"""
        alarm_id = event.alarm_id

        logger.info("Alarm acknowledged: %s", alarm_id)

        # Cancel any pending escalation for acknowledged alarms
        await self._cancel_escalation(alarm_id)

        # Get alarm details
        alarm = await self._repo.get(alarm_id)
        if alarm:
            # FIXED: acknowledged_by 值之前被丢弃，现在保留用于通知
            ack_by = alarm.get("acknowledged_by", "unknown")
            alarm["acknowledged_by"] = ack_by
            # FIXED-BugR14: 补充 rule_name/device_name，避免确认通知显示 "Unknown Rule"
            if not alarm.get("rule_name"):
                alarm["rule_name"] = event.rule_name or event.rule_id
            if not alarm.get("device_name"):
                alarm["device_name"] = event.device_name or event.device_id
            await self._send_notification(alarm, "acknowledged")

        # Update statistics
        # FIXED(严重): 加 _stats_lock 保护并发修改
        async with self._stats_lock:
            self._stats.acknowledged_count += 1

    async def _handle_escalation(self, event: AlarmEvent) -> None:
        """Handle alarm escalation event — update stats and notify"""
        alarm_id = event.alarm_id
        logger.info("Alarm escalated: %s", alarm_id)

        # Get alarm details for notification
        alarm = await self._repo.get(alarm_id)
        if alarm:
            # FIXED(P2): 原问题-升级通知未补充 rule_name/device_name，导致显示"Unknown Rule";
            # 修复-与 _handle_recovery/_handle_acknowledgment 保持一致，从事件补充名称
            if not alarm.get("rule_name"):
                alarm["rule_name"] = event.rule_name or event.rule_id
            if not alarm.get("device_name"):
                alarm["device_name"] = event.device_name or event.device_id
            await self._send_notification(alarm, "escalated")

        # Update statistics
        # FIXED(严重): 加 _stats_lock 保护并发修改
        async with self._stats_lock:
            self._stats.escalated_count += 1

    # ============== Escalation ==============

    async def _schedule_escalation(
        self,
        alarm_id: str,
        severity: str,
        alarm: dict,
    ) -> None:
        """Schedule alarm escalation based on severity threshold"""
        config = self._escalation_configs.get(severity)
        if not config:
            return

        # FIXED(严重): 加 _escalation_lock 保护"取消旧任务+创建新任务"流程，
        # 避免并发调用交错产生孤儿任务导致重复告警升级
        # 并发安全: 锁内仅 pop+cancel+create，锁外 await task 终止，避免持锁期间 await 阻塞其他协程
        old_task: asyncio.Task | None = None
        async with self._escalation_lock:
            # Cancel existing escalation if any (仅 pop+cancel，await 在锁外执行)
            old_task = self._escalation_tasks.pop(alarm_id, None)
            if old_task and not old_task.done():
                old_task.cancel()

            # Create escalation task
            # FIXED(一般): 原问题-_do_escalate协程无try-except保护，异常静默失败且_escalation_tasks残留;
            # 修复-添加try-except包裹，记录日志并在finally中清理_escalation_tasks
            async def _do_escalate():
                try:
                    await asyncio.sleep(config.threshold_seconds)

                    # Check if alarm is still firing
                    current_alarm = await self._repo.get(alarm_id)
                    if not current_alarm or current_alarm.get("status") != "firing":
                        logger.debug("Alarm %s no longer firing, skipping escalation", alarm_id)
                        return

                    # Perform escalation
                    await self._escalate_alarm(alarm_id, severity, config.escalate_to, alarm)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error("Alarm escalation failed for %s: %s", alarm_id, e)
                finally:
                    # FIXED(严重): 原问题-finally 误删 _escalate_alarm 设置的下一轮 task
                    # 修复-仅当当前 task 仍是 _escalation_tasks 中存储的 task 时才弹出
                    if self._escalation_tasks.get(alarm_id) is asyncio.current_task():
                        self._escalation_tasks.pop(alarm_id, None)

            task = asyncio.create_task(_do_escalate(), name=f"escalate-{alarm_id}")
            self._escalation_tasks[alarm_id] = task

        # 锁外等待旧任务终止完成，避免持锁期间 await 阻塞其他协程
        if old_task and not old_task.done():
            with contextlib.suppress(asyncio.CancelledError):
                await old_task

        logger.debug(
            "Escalation scheduled for alarm %s: severity=%s, threshold=%ds",
            alarm_id,
            severity,
            config.threshold_seconds,
        )

    async def _cancel_escalation(self, alarm_id: str) -> None:
        """Cancel pending escalation for an alarm

        R7-S-04 修复(严重): 原 _cancel_escalation 未使用 _escalation_lock，
        与 _schedule_escalation 的 _escalation_lock 保护构成竞态：
        cancel pop 了旧 task，schedule 同时创建新 task，两者交叉导致升级任务泄漏。
        修复-加 _escalation_lock 保护 pop+cancel 原子性。
        """
        async with self._escalation_lock:
            task = self._escalation_tasks.pop(alarm_id, None)
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _escalate_alarm(
        self,
        alarm_id: str,
        current_severity: str,
        target_severity: str,
        alarm: dict,
    ) -> None:
        """Perform alarm escalation"""
        logger.warning("Alarm escalated: %s (%s -> %s)", alarm_id, current_severity, target_severity)

        # R9-S-02 修复: 升级前保存原始严重度（仅首次升级时保存，保留最原始值），
        # 以便告警恢复时通过 _handle_recovery 还原为原始 severity
        # 并发安全: 加锁保护 _original_severities 的 check+set 操作
        async with self._original_severities_lock:
            if alarm_id not in self._original_severities:
                self._original_severities[alarm_id] = current_severity

        # Update alarm severity in database
        await self._repo.update_severity(alarm_id, target_severity)

        # FIXED: 通过 EventBus 发布告警升级事件，由 _handle_escalation 统一处理通知+统计
        # 不再直接调用 _send_notification，避免重复
        # FIXED(严重): 原问题-update_severity 成功但 publish 失败时，升级链断裂：
        # 无通知、无统计、无下一轮升级调度。修复-publish 失败不阻断后续调度，
        # 仅记录错误日志，保证升级链连续性
        if self._event_bus:
            rule_id = alarm.get("rule_id") or ""
            device_id = alarm.get("device_id") or ""
            rule_name, device_name = await self._resolve_names(rule_id, device_id)
            event = AlarmEvent(
                alarm_id=alarm_id,
                rule_id=rule_id,
                rule_name=rule_name,
                device_id=device_id,
                device_name=device_name,
                severity=target_severity,
                action="escalated",
            )
            try:
                await self._event_bus.publish(event)
            except Exception as publish_err:
                logger.error(
                    "Alarm %s escalation event publish failed (DB already updated): %s",
                    alarm_id,
                    publish_err,
                )
        else:
            # Fallback: 无 EventBus 时直接发送通知
            alarm["original_severity"] = current_severity
            # FIXED-P1: 原问题-escalation_level硬编码为1；改为基于当前级别递增
            _current_level = alarm.get("escalation_level", 0)
            alarm["escalation_level"] = _current_level + 1
            await self._send_notification(
                alarm,
                "escalated",
                escalation_level=_current_level + 1,
                original_severity=current_severity,
            )

        # Schedule next escalation if not at critical
        if target_severity != SEVERITY_CRITICAL:
            next_config = self._escalation_configs.get(target_severity)
            if next_config:
                # Create new alarm dict with escalated severity
                escalated_alarm = dict(alarm)
                escalated_alarm["severity"] = target_severity
                # FIXED-P1: 原问题-escalation_level硬编码为1，多轮升级后级别不递增；改为基于当前级别递增
                escalated_alarm["escalation_level"] = alarm.get("escalation_level", 0) + 1

                # Schedule next escalation
                async def _do_next_escalate():
                    try:
                        await asyncio.sleep(next_config.threshold_seconds)
                        current = await self._repo.get(alarm_id)
                        if current and current.get("status") in ("firing", "acknowledged"):
                            await self._escalate_alarm(
                                alarm_id,
                                target_severity,
                                next_config.escalate_to,
                                escalated_alarm,
                            )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.error("Next escalation failed for alarm %s", alarm_id, exc_info=True)
                    finally:
                        # FIXED-P1: 原问题-异常路径下_escalation_tasks残留alarm_id条目，
                        # 导致内存累积和后续_schedule_escalation的_cancel_escalation操作无效
                        # FIXED(严重): 原问题-finally 误删 _escalate_alarm 设置的下一轮 task
                        # 修复-仅当当前 task 仍是 _escalation_tasks 中存储的 task 时才弹出
                        if self._escalation_tasks.get(alarm_id) is asyncio.current_task():
                            self._escalation_tasks.pop(alarm_id, None)

                task = asyncio.create_task(_do_next_escalate(), name=f"escalate-{alarm_id}")
                # FIX-P1: 原代码写入 self._escalation_tasks[alarm_id] 未获取
                # _escalation_lock，与 _schedule_escalation/_cancel_escalation 并发时
                # 产生竞态。改为在 _escalation_lock 临界区内取消旧任务并写入新任务，
                # 与 _schedule_escalation 的保护模式保持一致。
                async with self._escalation_lock:
                    old_task = self._escalation_tasks.pop(alarm_id, None)
                    if old_task and not old_task.done():
                        old_task.cancel()
                    self._escalation_tasks[alarm_id] = task

    def configure_escalation(
        self,
        severity: str,
        threshold_seconds: int,
        escalate_to: str | None = None,
    ) -> None:
        """Configure escalation threshold for a severity level"""
        self._escalation_configs[severity] = AlarmEscalationConfig(
            severity=severity,
            threshold_seconds=threshold_seconds,
            escalate_to=escalate_to or severity,
        )
        logger.info(
            "Escalation configured: severity=%s, threshold=%ds, escalate_to=%s",
            severity,
            threshold_seconds,
            escalate_to or severity,
        )

    # ============== Suppression ==============

    def add_suppression_rule(self, rule: AlarmSuppressionRule) -> None:
        """Add an alarm suppression rule"""
        self._suppression_rules.append(rule)
        logger.info("Suppression rule added: %s (%s)", rule.name, rule.rule_id)

    def remove_suppression_rule(self, rule_id: str) -> bool:
        """Remove an alarm suppression rule"""
        for i, rule in enumerate(self._suppression_rules):
            if rule.rule_id == rule_id:
                del self._suppression_rules[i]
                logger.info("Suppression rule removed: %s", rule_id)
                return True
        return False

    def list_suppression_rules(self) -> list[AlarmSuppressionRule]:
        """List all suppression rules"""
        return list(self._suppression_rules)

    async def _is_suppressed(
        self,
        rule_id: str,
        device_id: str,
        severity: str,
    ) -> bool:
        """Check if an alarm should be suppressed"""
        now = datetime.now(UTC)
        # 业务决策：静默规则的时间窗口（time_range_start/end）按本地时间配置，
        # 例如用户配置 22:00-06:00 夜间抑制，应按本地时间生效。
        # 因此 current_time_str 使用本地时间（datetime.now()）而非 UTC，
        # 这与项目其他模块统一使用 datetime.now(UTC) 不同，但符合业务语义。
        # 注意：上面的 now（UTC）仅用于 expires_at 过期判断，与时区无关的比较。
        current_time_str = datetime.now().strftime("%H:%M")

        for rule in self._suppression_rules:
            if not rule.enabled:
                continue

            # FIXED-P0: 检查抑制规则是否已过期，过期的规则不再生效
            if rule.expires_at is not None and now > rule.expires_at:
                logger.debug("Suppression rule %s expired, skipping", rule.rule_id)
                continue

            # Check device filter
            if rule.device_ids and device_id not in rule.device_ids:
                continue

            # Check rule filter
            if rule.rule_ids and rule_id not in rule.rule_ids:
                continue

            # Check severity filter
            if rule.severities and severity not in rule.severities:
                continue

            # Check time range
            if rule.time_range_start and rule.time_range_end:
                if not self._is_in_time_range(
                    current_time_str,
                    rule.time_range_start,
                    rule.time_range_end,
                ):
                    continue

            # All conditions match - alarm is suppressed
            logger.debug(
                "Alarm suppressed by rule %s: rule_id=%s, device_id=%s, severity=%s",
                rule.rule_id,
                rule_id,
                device_id,
                severity,
            )
            return True

        return False

    @staticmethod
    def _is_in_time_range(current: str, start: str, end: str) -> bool:
        """Check if current time is within the specified range"""
        try:
            # FIXED-P1: 规范化时间格式，避免 "9:00" vs "09:00" 比较错误
            def _normalize(t: str) -> str:
                parts = t.strip().split(":")
                if len(parts) != 2:
                    return t
                return f"{int(parts[0]):02d}:{int(parts[1]):02d}"

            current = _normalize(current)
            start = _normalize(start)
            end = _normalize(end)

            # Handle overnight ranges (e.g., 22:00 - 06:00)
            if start > end:
                return current >= start or current <= end
            return start <= current <= end
        except (ValueError, IndexError):
            return False

    # ============== Statistics ==============

    @property
    def _recovery_times(self) -> list[float]:
        """Get recovery times for MTTR calculation"""
        return self._stats.recovery_times

    @_recovery_times.setter
    def _recovery_times(self, value: list[float]) -> None:
        self._stats.recovery_times = value

    @property
    def _failure_intervals(self) -> list[float]:
        """Get failure intervals for MTBF calculation"""
        return self._stats.failure_intervals

    @_failure_intervals.setter
    def _failure_intervals(self, value: list[float]) -> None:
        self._stats.failure_intervals = value

    def _update_mttr_mtbf(self) -> None:
        """Update MTTR and MTBF calculations"""
        # Calculate MTTR
        if self._stats.recovery_times:
            self._stats.mttr_seconds = sum(self._stats.recovery_times) / len(self._stats.recovery_times)
        else:
            self._stats.mttr_seconds = 0.0

        # Calculate MTBF
        if self._stats.failure_intervals:
            self._stats.mtbf_seconds = sum(self._stats.failure_intervals) / len(self._stats.failure_intervals)
        else:
            self._stats.mtbf_seconds = 0.0

    def get_statistics(self) -> AlarmStatistics:
        """Get current alarm statistics"""
        self._update_mttr_mtbf()
        return self._stats

    async def get_statistics_summary(
        self, device_ids: set[str] | None = None
    ) -> dict[str, Any]:  # FIXED-P0: 改为async以支持await调用
        self._update_mttr_mtbf()

        if device_ids is not None:
            return await self._get_filtered_statistics(device_ids)

        # FIXED-P2: W24 device_ids=None时从数据库重新计算统计而非直接返回内存值
        return await self._get_filtered_statistics(None)

    async def _get_filtered_statistics(
        self, device_ids: set[str] | None
    ) -> dict[str, Any]:  # FIXED-P0: _get_filtered_statistics使用正确的属性名
        # FIXED(一般): 原问题-3次全量加载(每次最多_MAX_QUERY_SIZE条)后内存统计，超限被截断且开销大;
        # 修复-改用count_by_status_and_severity单条GROUP BY查询，避免全量加载与内存过滤
        device_ids_list = list(device_ids) if device_ids is not None else None
        counts = await self._repo.count_by_status_and_severity(device_ids=device_ids_list)
        total = 0
        firing = 0
        acked = 0
        recovered = 0
        by_severity: dict[str, int] = {}
        # 仅统计 firing/acknowledged/recovered 三种状态，与原逻辑保持一致
        for (status, sev), cnt in counts.items():
            if status == "firing":
                firing += cnt
                total += cnt
            elif status == "acknowledged":
                acked += cnt
                total += cnt
            elif status == "recovered":
                recovered += cnt
                total += cnt
            else:
                # 其他状态不计入 total，与原逻辑一致(原逻辑仅查询三种状态)
                continue
            by_severity[sev] = by_severity.get(sev, 0) + cnt
        return {
            "total_alarms": total,
            "firing_alarms": firing,
            "acknowledged_alarms": acked,
            "recovered_alarms": recovered,
            "by_severity": by_severity,
            # FIXED-P0: 原代码硬编码 mttr_seconds/mtbf_seconds 为 0，导致前端永远显示 0s
            # 改为使用实际计算的 MTTR/MTBF 值
            "mttr_seconds": self._stats.mttr_seconds,
            "mttr_formatted": self._format_duration(self._stats.mttr_seconds),
            "mtbf_seconds": self._stats.mtbf_seconds,
            # FIXED-P2: 原问题-注释说"误用mttr_seconds"但代码实际未修复
            # 导致前端 MTBF 始终等于 MTTR
            "mtbf_formatted": self._format_duration(self._stats.mtbf_seconds),
            "alarm_rate_per_hour": self._calculate_alarm_rate(),
        }

    def _calculate_alarm_rate(self) -> float:
        """Calculate alarm rate per hour"""
        # FIXED(P2): 原问题-依赖recovery_times(恢复后才非空)和_alarm_start_times(恢复后清空)，
        # 导致(1)无恢复时返回0 (2)全部恢复后返回0 (3)分子用恢复次数而非总告警次数;
        # 修复-用_first_alarm_time(首次告警时间,不受恢复影响)作窗口起点,用total_count(总告警数)作分子
        if not self._first_alarm_time or self._stats.total_count == 0:
            return 0.0
        now = datetime.now(UTC)
        window_seconds = (now - self._first_alarm_time).total_seconds()
        if window_seconds <= 0:
            return 0.0
        return self._stats.total_count / (window_seconds / 3600)

    @staticmethod
    def _format_duration(seconds: float) -> str:
        """Format duration in human-readable format"""
        if seconds < 60:
            return f"{seconds:.0f}s"
        elif seconds < 3600:
            return f"{seconds / 60:.1f}m"
        elif seconds < 86400:
            return f"{seconds / 3600:.1f}h"
        else:
            return f"{seconds / 86400:.1f}d"

    async def get_trend(self, hours: int = 24, device_ids: set[str] | None = None) -> dict[str, Any]:
        """Get alarm trend data for the specified number of hours"""
        data = await self._repo.query_trend_data(hours)
        if device_ids is not None:
            data["top_devices"] = [d for d in data.get("top_devices", []) if d.get("device_id") in device_ids]
        return data

    async def get_top_alarms(
        self,
        hours: int = 24,
        device_ids: set[str] | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """修复7: 获取 Top N 报警设备/规则排名，用于 Top10 看板"""
        device_ids_list = list(device_ids) if device_ids is not None else None
        return await self._repo.get_top_alarms(hours=hours, device_ids=device_ids_list, limit=limit)

    # ============== Cleanup ==============

    async def _cleanup_loop(self) -> None:
        """Periodic cleanup of old statistics and expired alarm records"""
        while True:
            try:
                await asyncio.sleep(3600)  # Run every hour
                await self._cleanup_old_data()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in alarm cleanup: %s", e)

    async def _cleanup_old_data(self) -> None:
        """Clean up old statistics data and expired alarm records"""
        # FIXED(严重): 加 _stats_lock 保护所有统计数据修改，避免与并发告警事件 handler 竞态
        async with self._stats_lock:
            # Keep only last 100 recovery times
            if len(self._stats.recovery_times) > 100:
                self._stats.recovery_times = self._stats.recovery_times[-100:]

            # Keep only last 100 failure intervals
            if len(self._stats.failure_intervals) > 100:
                self._stats.failure_intervals = self._stats.failure_intervals[-100:]

            # FIXED-P0: 清理过期的 _alarm_start_times 条目，防止内存泄漏
            # 告警开始时间超过 7 天仍未恢复的，视为孤儿数据，清理掉
            cutoff_ts = datetime.now(UTC).timestamp() - 7 * 86400
            stale_keys = [aid for aid, st in self._alarm_start_times.items() if st.timestamp() < cutoff_ts]
            for aid in stale_keys:
                del self._alarm_start_times[aid]
            if stale_keys:
                logger.warning("Cleaned up %d stale alarm start times (orphaned >7d)", len(stale_keys))

        # Clean up old alarm records from SQLite (default 90 days retention)
        try:
            deleted = await self._repo.cleanup_old_alarms(retention_days=90)
            if deleted > 0:
                logger.info("Cleaned up %d old alarm records from database", deleted)
        except Exception as e:
            logger.error("Failed to cleanup old alarm records: %s", e)

        logger.debug("Alarm statistics cleanup completed")

    # ============== Public API ==============

    async def list_alarms(
        self,
        page: int = 1,
        size: int = 20,
        status: str | None = None,
        severity: str | None = None,
        device_id: str | None = None,
        search: str | None = None,
        device_ids: set[str] | None = None,
    ) -> tuple[list[dict], int]:
        # FIXED-P1: 当 device_ids 过滤时，原代码将 total 设为当前页过滤后的数量，
        # 导致前端分页错误。改为查询全量匹配记录后正确计算 total 并手动分页。
        if device_ids is not None:
            # FIXED(一般): 原问题-传入device_ids时先加载_MAX_QUERY_SIZE条再内存过滤，结果可能不完整;
            # 修复-将device_ids下推到SQL，由数据库完成过滤与分页，避免截断
            # FIXED-mypy: list_all 返回 tuple[list, int] | tuple[list, int, str|None] 联合类型，使用索引避免解包歧义
            _result = await self._repo.list_all(
                page,
                size,
                status,
                severity,
                device_id,
                search,
                device_ids=list(device_ids),
            )
            return _result[0], _result[1]

        # FIXED-mypy: list_all 返回 tuple[list, int] | tuple[list, int, str|None] 联合类型，使用索引避免解包歧义
        _result = await self._repo.list_all(page, size, status, severity, device_id, search)
        return _result[0], _result[1]

    async def get_alarm(self, alarm_id: str) -> dict | None:
        return await self._repo.get(alarm_id)

    async def get_alarm_history(self, rule_id: str, days: int = 7) -> list[dict]:
        """修复9: 查询指定规则最近 N 天的历史触发记录"""
        return await self._repo.get_alarm_history(rule_id, days=days)

    async def delete_alarm(self, alarm_id: str) -> bool:
        """FIXED(严重): 物理删除告警记录，仅 admin 可调用。

        清理内存中的升级任务和告警开始时间，避免残留引用。
        R7-S-03/S-04 修复: 完整清理 _original_severities/_handled_alarm_ids/_alarm_locks/_escalation_tasks，
        并使用对应锁保护每个集合的并发访问。
        """
        # 清理内存状态
        # FIXED(严重): _alarm_start_times.pop 加 _stats_lock，与其他方法中的锁保护保持一致
        async with self._stats_lock:
            self._alarm_start_times.pop(alarm_id, None)
        # R9-S-02: 清理升级前保存的原始严重度，避免内存泄漏
        # R7-S-04 修复: 加 _original_severities_lock 保护 pop 操作
        async with self._original_severities_lock:
            self._original_severities.pop(alarm_id, None)
        # R7-S-04 修复: 使用 _cancel_escalation 替代直接 pop _escalation_tasks，
        # 确保与 _schedule_escalation 的 _escalation_lock 保护一致
        await self._cancel_escalation(alarm_id)
        # R7-S-03 修复: 清理 _handled_alarm_ids 和 _alarm_locks，防止内存泄漏
        async with self._handled_alarm_ids_lock:
            self._handled_alarm_ids.discard(alarm_id)
        async with self._alarm_locks_meta:
            self._alarm_locks.pop(alarm_id, None)
        return await self._repo.delete(alarm_id)

    async def ack_alarm(self, alarm_id: str, ack_by: str) -> dict | None:
        """Acknowledge an alarm"""
        alarm = await self._repo.ack(alarm_id, ack_by)
        if alarm:
            # FIXED-BugR13: 状态冲突（已被他人确认）时不发布事件，避免重复确认通知
            if alarm.pop("_status_conflict", None):
                return alarm
            rule_id = alarm.get("rule_id") or ""
            device_id = alarm.get("device_id") or ""
            # FIXED: 查询 rule_name 和 device_name，避免前端显示"未知规则: 未知设备"
            rule_name, device_name = await self._resolve_names(rule_id, device_id)
            event = AlarmEvent(
                alarm_id=alarm_id,
                rule_id=rule_id,
                rule_name=rule_name,
                device_id=device_id,
                device_name=device_name,
                severity=alarm.get("severity", SEVERITY_INFO),
                action="acknowledged",
            )
            # FIXED: 通过 EventBus 发布事件，handler 会自动处理（通知+统计+前端推送）
            # 避免直接调用 _handle_acknowledgment 导致重复处理
            if self._event_bus:
                await self._event_bus.publish(event)
            else:
                await self._handle_acknowledgment(event)
        return alarm

    async def trigger_alarm(
        self,
        rule_id: str,
        rule_name: str,
        device_id: str,
        device_name: str,
        severity: str,
        message: str,
        trigger_value: dict,
        channels: list[str] | None = None,
    ) -> dict | None:
        """Manually trigger an alarm"""
        # FIXED-P1: 手动触发的告警也应受抑制规则约束，避免告警风暴绕过抑制
        if await self._is_suppressed(rule_id, device_id, severity):
            logger.info("Manually triggered alarm suppressed: rule=%s, device=%s", rule_id, device_id)
            return None

        # Create alarm record
        alarm_data = {
            "rule_id": rule_id,
            "device_id": device_id,
            "severity": severity,
            "message": message,
            "trigger_value": trigger_value,
            "rule_type": "manual",
        }

        try:
            alarm = await self._repo.create(alarm_data)
            if alarm:
                # Send notification
                # FIXED(严重): 原问题-通知失败时告警已持久化但无通知发出，形成"孤儿告警"
                # 修复：记录通知失败日志，便于运维排查；通知失败不阻断告警创建
                _notify_results = await self._send_notification(alarm, "firing", channels)
                _failed_channels = [ch for ch, ok in _notify_results.items() if not ok]
                if _failed_channels:
                    logger.warning(
                        "Manually triggered alarm %s notification failed on channels: %s "
                        "(alarm persisted, manual retry may be needed)",
                        alarm.get("alarm_id"),
                        _failed_channels,
                    )
            return alarm
        except Exception as e:
            logger.error("Failed to trigger alarm: %s", e, exc_info=True)
            return None

    async def clear_alarm(self, alarm_id: str) -> dict | None:
        """Clear/recover an alarm"""
        alarm = await self._repo.recover(alarm_id)
        if alarm:
            # FIXED-BugR13: 状态冲突（已恢复）时不发布事件，避免重复恢复通知
            if alarm.pop("_status_conflict", None):
                return alarm
            rule_id = alarm.get("rule_id") or ""
            device_id = alarm.get("device_id") or ""
            # FIXED: 查询 rule_name 和 device_name，避免前端显示"未知规则: 未知设备"
            rule_name, device_name = await self._resolve_names(rule_id, device_id)
            event = AlarmEvent(
                alarm_id=alarm_id,
                rule_id=rule_id,
                rule_name=rule_name,
                device_id=device_id,
                device_name=device_name,
                severity=alarm.get("severity", SEVERITY_INFO),
                action="recovered",
            )
            # FIXED: 通过 EventBus 发布事件，handler 会自动处理（通知+统计+前端推送）
            # 避免直接调用 _handle_recovery 导致重复处理
            if self._event_bus:
                await self._event_bus.publish(event)
            else:
                await self._handle_recovery(event)
        return alarm

    async def test_notification_channel(
        self,
        channel_id: str,
    ) -> tuple[bool, str]:
        """Test a notification channel"""
        return await self._notification_manager.test_channel(channel_id)

    def list_notification_channels(self) -> list[dict]:
        """List all notification channels"""
        return self._notification_manager.list_channels()

    def set_notification_channel_enabled(
        self,
        channel_id: str,
        enabled: bool,
    ) -> bool:
        """Enable or disable a notification channel"""
        return self._notification_manager.set_channel_enabled(channel_id, enabled)
