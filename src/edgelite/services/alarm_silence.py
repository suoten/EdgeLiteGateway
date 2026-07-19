"""告警静默（维护窗口）管理服务

提供告警静默规则的 CRUD 操作，支持：
- 全局静默（device_id 为空）
- 设备级静默
- 规则级静默

所有数据持久化在主数据库的 alarm_silences 表中。
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from edgelite.models.db import AlarmSilenceORM

logger = logging.getLogger(__name__)


def _parse_iso_time(time_str: str) -> datetime | None:
    """解析 ISO 格式时间字符串，空则返回 None。"""
    if not time_str:
        return None
    try:
        dt = datetime.fromisoformat(time_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (ValueError, TypeError):
        return None


def _orm_to_dict(orm: AlarmSilenceORM) -> dict[str, Any]:
    """将 ORM 对象转为字典。"""
    return {
        "id": orm.id,
        "device_id": orm.device_id or "",
        "rule_id": orm.rule_id or "",
        "start_time": orm.start_time.isoformat() if orm.start_time else None,
        "end_time": orm.end_time.isoformat() if orm.end_time else None,
        "reason": orm.reason or "",
        "operator": orm.operator or "system",
        "created_at": orm.created_at.isoformat() if orm.created_at else None,
        "cancelled_at": getattr(orm, "cancelled_at", None),
    }


class AlarmSilenceManager:
    """告警静默管理器

    通过数据库 session 进行静默规则的 CRUD 操作。
    """

    def __init__(self, database=None) -> None:
        self._db = database

    def init(self, database) -> None:
        """初始化管理器，注入数据库实例。"""
        self._db = database
        logger.info("AlarmSilenceManager initialized")

    def _get_session(self) -> AsyncSession:
        """获取数据库 session。"""
        if self._db is None:
            raise RuntimeError("AlarmSilenceManager not initialized: database is None")
        return self._db.get_session()

    async def list_silences(
        self,
        device_id: str = "",
        rule_id: str = "",
        active_only: bool = True,
    ) -> list[dict[str, Any]]:
        """查询静默规则列表。"""
        try:
            async with self._get_session() as session:
                stmt = select(AlarmSilenceORM).order_by(AlarmSilenceORM.start_time.desc())
                if device_id:
                    stmt = stmt.where(AlarmSilenceORM.device_id == device_id)
                if rule_id:
                    stmt = stmt.where(AlarmSilenceORM.rule_id == rule_id)
                result = await session.execute(stmt)
                rows = result.scalars().all()

                silences = [_orm_to_dict(r) for r in rows]
                if active_only:
                    now = datetime.now(UTC)
                    silences = [
                        s for s in silences
                        if _parse_iso_time(s.get("end_time", "")) is not None
                        and _parse_iso_time(s["end_time"]) > now
                        and not s.get("cancelled_at")
                    ]
                return silences
        except Exception as e:
            logger.error("list_silences failed: %s", e)
            raise

    async def get_silence_by_id(self, silence_id: str) -> dict[str, Any] | None:
        """根据ID查询单条静默规则。"""
        try:
            async with self._get_session() as session:
                stmt = select(AlarmSilenceORM).where(AlarmSilenceORM.id == silence_id)
                result = await session.execute(stmt)
                row = result.scalar_one_or_none()
                if row is None:
                    return None
                return _orm_to_dict(row)
        except Exception as e:
            logger.error("get_silence_by_id failed: %s", e)
            raise

    async def create_silence(
        self,
        device_id: str = "",
        rule_id: str = "",
        start_time: str = "",
        end_time: str = "",
        reason: str = "",
        operator: str = "system",
    ) -> dict[str, Any]:
        """创建静默规则。"""
        now = datetime.now(UTC)
        start_dt = _parse_iso_time(start_time) or now
        end_dt = _parse_iso_time(end_time) or (now + timedelta(hours=1))

        silence_id = str(uuid.uuid4())
        try:
            async with self._get_session() as session:
                orm = AlarmSilenceORM(
                    id=silence_id,
                    device_id=device_id or None,
                    rule_id=rule_id or None,
                    start_time=start_dt,
                    end_time=end_dt,
                    reason=reason,
                    operator=operator,
                    created_at=now,
                )
                session.add(orm)
                await session.commit()
                logger.info(
                    "Alarm silence created: id=%s, device=%s, rule=%s, operator=%s",
                    silence_id, device_id or "*", rule_id or "*", operator,
                )
                return _orm_to_dict(orm)
        except Exception as e:
            logger.error("create_silence failed: %s", e)
            raise

    async def delete_silence(self, silence_id: str) -> bool:
        """删除静默规则。"""
        try:
            async with self._get_session() as session:
                stmt = delete(AlarmSilenceORM).where(AlarmSilenceORM.id == silence_id)
                result = await session.execute(stmt)
                await session.commit()
                deleted = result.rowcount > 0
                if deleted:
                    logger.info("Alarm silence deleted: id=%s", silence_id)
                return deleted
        except Exception as e:
            logger.error("delete_silence failed: %s", e)
            raise

    def is_silenced(
        self,
        silences: list[dict[str, Any]],
        device_id: str,
        rule_id: str,
    ) -> bool:
        """检查给定的设备/规则是否在静默窗口内（内存判断，不查库）。"""
        now = datetime.now(UTC)
        for s in silences:
            end_dt = _parse_iso_time(s.get("end_time", ""))
            start_dt = _parse_iso_time(s.get("start_time", ""))
            if start_dt and now < start_dt:
                continue
            if end_dt and now > end_dt:
                continue
            s_device = s.get("device_id", "")
            s_rule = s.get("rule_id", "")
            if not s_device and not s_rule:
                return True
            if s_device and s_device == device_id:
                if not s_rule or s_rule == rule_id:
                    return True
            if s_rule and s_rule == rule_id and not s_device:
                return True
        return False


# 模块级单例
_manager: AlarmSilenceManager | None = None


def get_alarm_silence_manager(database=None) -> AlarmSilenceManager:
    """获取告警静默管理器单例。

    Args:
        database: 可选的数据库实例。首次调用时传入以初始化，
                  后续调用可不传（使用已初始化的实例）。

    Returns:
        AlarmSilenceManager 实例
    """
    global _manager
    if _manager is None:
        _manager = AlarmSilenceManager(database)
        if database is not None:
            _manager.init(database)
    elif database is not None and _manager._db is None:
        _manager.init(database)
    return _manager
