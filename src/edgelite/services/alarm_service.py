"""Enhanced alarm service with notification, escalation, suppression and statistics

This module provides comprehensive alarm management capabilities including:
- Multi-channel notifications (DingTalk, WeCom, Email, Webhook)
- Alarm escalation based on duration thresholds
- Alarm suppression/masking rules
- Alarm statistics (MTTR, MTBF, trends)
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from edgelite.engine.event_bus import AlarmEvent, EventBus, PointUpdateEvent
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
    device_ids: list[str] = field(default_factory=list)  # Empty = all devices
    rule_ids: list[str] = field(default_factory=list)  # Empty = all rules
    severities: list[str] = field(default_factory=list)  # Empty = all severities
    time_range_start: str = ""  # HH:MM format, empty = no start constraint
    time_range_end: str = ""  # HH:MM format, empty = no end constraint
    enabled: bool = True


@dataclass
class AlarmStatistics:
    """Alarm statistics"""
    total_count: int = 0
    firing_count: int = 0
    acknowledged_count: int = 0
    recovered_count: int = 0
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
    ):
        self._repo = alarm_repo
        self._event_bus = event_bus
        self._notification_manager = notification_manager or get_notification_manager()

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

        # Suppression rules
        self._suppression_rules: list[AlarmSuppressionRule] = []

        # Statistics
        self._stats = AlarmStatistics()
        self._alarm_start_times: dict[str, datetime] = {}
        self._last_recovery_time: datetime | None = None

        # Task for statistics cleanup
        self._cleanup_task: asyncio.Task | None = None

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
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._escalation_tasks.clear()

        # Cancel cleanup task
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

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
        except Exception as e:
            logger.error("Error handling alarm event: %s", e)

    async def handle_alarm_event(self, event: AlarmEvent) -> None:
        """Handle alarm firing event"""
        alarm_id = event.alarm_id
        rule_id = event.rule_id
        device_id = event.device_id
        severity = event.severity

        logger.info(
            "Alarm fired: %s (rule=%s, device=%s, severity=%s)",
            alarm_id, rule_id, device_id, severity
        )

        # Check suppression
        if await self._is_suppressed(rule_id, device_id, severity):
            logger.info("Alarm %s suppressed by rule", alarm_id)
            return

        # Get alarm details from database
        alarm = await self._repo.get(alarm_id)
        if not alarm:
            logger.warning("Alarm not found: %s", alarm_id)
            return

        # Send notification
        await self._send_notification(alarm, "firing")

        # Schedule escalation if configured
        await self._schedule_escalation(alarm_id, severity, alarm)

        # Update statistics
        self._alarm_start_times[alarm_id] = datetime.now(UTC)
        self._stats.firing_count += 1
        self._stats.total_count += 1
        self._stats.by_severity[severity] = self._stats.by_severity.get(severity, 0) + 1

    async def _handle_recovery(self, event: AlarmEvent) -> None:
        """Handle alarm recovery event"""
        alarm_id = event.alarm_id

        logger.info("Alarm recovered: %s", alarm_id)

        # Cancel any pending escalation
        await self._cancel_escalation(alarm_id)

        # Calculate duration
        start_time = self._alarm_start_times.get(alarm_id)
        duration_seconds = 0.0
        if start_time:
            duration_seconds = (datetime.now(UTC) - start_time).total_seconds()
            del self._alarm_start_times[alarm_id]

            # Record recovery time for MTTR calculation
            self._recovery_times.append(duration_seconds)
            if len(self._recovery_times) > 100:  # Keep last 100
                self._recovery_times = self._recovery_times[-100:]

            # Record failure interval for MTBF calculation
            if self._last_recovery_time:
                interval = (start_time - self._last_recovery_time).total_seconds()
                self._failure_intervals.append(interval)
                if len(self._failure_intervals) > 100:
                    self._failure_intervals = self._failure_intervals[-100:]

            self._last_recovery_time = datetime.now(UTC)

        # Get alarm details
        alarm = await self._repo.get(alarm_id)
        if alarm:
            alarm["duration_seconds"] = duration_seconds
            await self._send_notification(alarm, "recovered")

        # Update statistics
        self._stats.firing_count = max(0, self._stats.firing_count - 1)
        self._stats.recovered_count += 1
        self._update_mttr_mtbf()

    async def _handle_acknowledgment(self, event: AlarmEvent) -> None:
        """Handle alarm acknowledgment event"""
        alarm_id = event.alarm_id
        severity = event.severity

        logger.info("Alarm acknowledged: %s", alarm_id)

        # Cancel any pending escalation for acknowledged alarms
        await self._cancel_escalation(alarm_id)

        # Get alarm details
        alarm = await self._repo.get(alarm_id)
        if alarm:
            ack_by = alarm.get("acknowledged_by", "unknown")
            await self._send_notification(alarm, "acknowledged")

        # Update statistics
        self._stats.acknowledged_count += 1

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

        # Cancel existing escalation if any
        await self._cancel_escalation(alarm_id)

        # Create escalation task
        async def _do_escalate():
            await asyncio.sleep(config.threshold_seconds)

            # Check if alarm is still firing
            current_alarm = await self._repo.get(alarm_id)
            if not current_alarm or current_alarm.get("status") != "firing":
                logger.debug("Alarm %s no longer firing, skipping escalation", alarm_id)
                return

            # Perform escalation
            await self._escalate_alarm(alarm_id, severity, config.escalate_to, alarm)

        task = asyncio.create_task(_do_escalate(), name=f"escalate-{alarm_id}")
        self._escalation_tasks[alarm_id] = task
        logger.debug(
            "Escalation scheduled for alarm %s: severity=%s, threshold=%ds",
            alarm_id, severity, config.threshold_seconds
        )

    async def _cancel_escalation(self, alarm_id: str) -> None:
        """Cancel pending escalation for an alarm"""
        task = self._escalation_tasks.pop(alarm_id, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _escalate_alarm(
        self,
        alarm_id: str,
        current_severity: str,
        target_severity: str,
        alarm: dict,
    ) -> None:
        """Perform alarm escalation"""
        logger.warning(
            "Alarm escalated: %s (%s -> %s)",
            alarm_id, current_severity, target_severity
        )

        # Update alarm severity in database
        # Note: In production, you'd update the alarm record here

        # Send escalation notification
        alarm["original_severity"] = current_severity
        await self._send_notification(
            alarm,
            "escalated",
            escalation_level=1,
            original_severity=current_severity,
        )

        # Schedule next escalation if not at critical
        if target_severity != SEVERITY_CRITICAL:
            next_config = self._escalation_configs.get(target_severity)
            if next_config:
                # Create new alarm dict with escalated severity
                escalated_alarm = dict(alarm)
                escalated_alarm["severity"] = target_severity
                escalated_alarm["escalation_level"] = 1

                # Schedule next escalation
                async def _do_next_escalate():
                    await asyncio.sleep(next_config.threshold_seconds)
                    current = await self._repo.get(alarm_id)
                    if current and current.get("status") == "firing":
                        await self._escalate_alarm(
                            alarm_id,
                            target_severity,
                            next_config.escalate_to,
                            escalated_alarm,
                        )

                task = asyncio.create_task(_do_next_escalate(), name=f"escalate-{alarm_id}")
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
            severity, threshold_seconds, escalate_to or severity
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
        current_time_str = now.strftime("%H:%M")

        for rule in self._suppression_rules:
            if not rule.enabled:
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
                rule.rule_id, rule_id, device_id, severity
            )
            return True

        return False

    @staticmethod
    def _is_in_time_range(current: str, start: str, end: str) -> bool:
        """Check if current time is within the specified range"""
        try:
            # Handle overnight ranges (e.g., 22:00 - 06:00)
            if start > end:
                return current >= start or current <= end
            return start <= current <= end
        except ValueError:
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

    def get_statistics_summary(self) -> dict[str, Any]:
        """Get alarm statistics as a dictionary summary"""
        self._update_mttr_mtbf()

        return {
            "total_alarms": self._stats.total_count,
            "firing_alarms": self._stats.firing_count,
            "acknowledged_alarms": self._stats.acknowledged_count,
            "recovered_alarms": self._stats.recovered_count,
            "by_severity": dict(self._stats.by_severity),
            "mttr_seconds": round(self._stats.mttr_seconds, 2),
            "mttr_formatted": self._format_duration(self._stats.mttr_seconds),
            "mtbf_seconds": round(self._stats.mtbf_seconds, 2),
            "mtbf_formatted": self._format_duration(self._stats.mtbf_seconds),
            "alarm_rate_per_hour": self._calculate_alarm_rate(),
        }

    def _calculate_alarm_rate(self) -> float:
        """Calculate alarm rate per hour"""
        if not self._stats.recovery_times:
            return 0.0
        total_time = sum(self._stats.recovery_times)
        if total_time == 0:
            return 0.0
        return len(self._stats.recovery_times) / (total_time / 3600)

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

    async def get_trend(self, hours: int = 24) -> dict[str, Any]:
        """Get alarm trend data for the specified number of hours"""
        return await self._repo.query_trend_data(hours)

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
        # Keep only last 100 recovery times
        if len(self._stats.recovery_times) > 100:
            self._stats.recovery_times = self._stats.recovery_times[-100:]

        # Keep only last 100 failure intervals
        if len(self._stats.failure_intervals) > 100:
            self._stats.failure_intervals = self._stats.failure_intervals[-100:]

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
    ) -> tuple[list[dict], int]:
        return await self._repo.list_all(page, size, status, severity, device_id, search)

    async def get_alarm(self, alarm_id: str) -> dict | None:
        return await self._repo.get(alarm_id)

    async def ack_alarm(self, alarm_id: str, ack_by: str) -> dict | None:
        """Acknowledge an alarm"""
        alarm = await self._repo.ack(alarm_id, ack_by)
        if alarm:
            await self._handle_acknowledgment(
                AlarmEvent(
                    alarm_id=alarm_id,
                    rule_id=alarm.get("rule_id", ""),
                    device_id=alarm.get("device_id", ""),
                    severity=alarm.get("severity", SEVERITY_INFO),
                    action="acknowledged",
                )
            )
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
                await self._send_notification(alarm, "firing", channels)
            return alarm
        except Exception as e:
            logger.error("Failed to trigger alarm: %s", e)
            return None

    async def clear_alarm(self, alarm_id: str) -> dict | None:
        """Clear/recover an alarm"""
        alarm = await self._repo.recover(alarm_id)
        if alarm:
            await self._handle_recovery(
                AlarmEvent(
                    alarm_id=alarm_id,
                    rule_id=alarm.get("rule_id", ""),
                    device_id=alarm.get("device_id", ""),
                    severity=alarm.get("severity", SEVERITY_INFO),
                    action="recovered",
                )
            )
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
