"""告警处理业务逻辑"""

from __future__ import annotations

import logging

from edgelite.engine.event_bus import AlarmEvent
from edgelite.storage.sqlite_repo import AlarmRepo

logger = logging.getLogger(__name__)


class AlarmService:
    """告警处理业务逻辑"""

    def __init__(self, alarm_repo: AlarmRepo):
        self._repo = alarm_repo

    async def handle_alarm_event(self, event: AlarmEvent) -> None:
        """处理告警事件（由EventBus调用）"""
        if event.action == "firing":
            logger.info(
                "告警触发: %s (规则=%s, 设备=%s)", event.alarm_id, event.rule_id, event.device_id
            )
        elif event.action == "recovered":
            logger.info("告警恢复: %s", event.alarm_id)

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
        return await self._repo.ack(alarm_id, ack_by)
