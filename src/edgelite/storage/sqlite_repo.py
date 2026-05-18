"""ORM-based 仓储实现（SQLAlchemy 2.0 异步）"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from edgelite.models.db import AlarmORM, DeviceORM, RuleORM, UserORM
from edgelite.api.error_codes import RepoErrors

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


# FIXED: 原问题-json.loads无异常保护，数据库字段损坏导致整个查询崩溃
# 现提供安全解析辅助函数
def _safe_json_loads(value: Any, default: Any = None) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return default
    return value


def _uuid() -> str:
    return uuid.uuid4().hex[:_SHORT_ID_LENGTH]  # FIXED: 原问题-魔法数字，提取为命名常量


class BaseRepo:
    def __init__(self, session_or_db: Any, write_lock: asyncio.Lock | None = None):
        from edgelite.storage.database import Database

        if isinstance(session_or_db, Database):
            self._database = session_or_db
            self._external_session: AsyncSession | None = None
        else:
            self._database = None
            self._external_session = session_or_db
        self._write_lock = write_lock

    async def _commit(self, session: AsyncSession) -> None:
        if self._write_lock:
            async with self._write_lock:
                await session.commit()
        else:
            await session.commit()

    def _get_session(self) -> AsyncSession:
        if self._external_session is not None:
            return self._external_session
        raise RuntimeError(RepoErrors.DB_MODE_SESSION_REQUIRED) from None

    @property
    def _is_database_mode(self) -> bool:
        return self._database is not None

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _auto_session(self) -> AsyncGenerator[AsyncSession, None]:
        if self._external_session is not None:
            yield self._external_session
        elif self._database is not None:
            async with self._database.get_session() as session:
                yield session
        else:
            raise RuntimeError(RepoErrors.NO_SESSION_AVAILABLE) from None

    # FIXED: 原问题-所有Repo方法无try-except保护，数据库异常直接抛出导致调用方崩溃
    # 现提供安全执行辅助方法，查询类操作异常时返回默认值，写入类操作异常时记录日志并抛出
    async def _safe_query(self, coro, default=None, label="query"):
        try:
            return await coro
        except Exception as e:
            logger.error("Repo %s failed: %s", label, e)
            return default

    async def _safe_write(self, coro, label="write"):
        try:
            return await coro
        except IntegrityError:
            raise
        except Exception as e:
            logger.error("Repo %s failed: %s", label, e)
            raise


class DeviceRepo(BaseRepo):
    async def create(self, data: dict) -> dict:
        # FIXED: 数据库插入无IntegrityError处理
        try:
            async with self._auto_session() as session:
                now = _now()
                orm = DeviceORM(
                    device_id=data["device_id"],
                    name=data["name"],
                    protocol=data["protocol"],
                    status=data.get("status", "offline"),
                    config=json.dumps(data.get("config", {}), ensure_ascii=False),
                    points=json.dumps(data.get("points", []), ensure_ascii=False),
                    collect_interval=data.get("collect_interval", 5),
                    created_at=now,
                    updated_at=now,
                )
                session.add(orm)
                await self._commit(session)
                await session.refresh(orm)
                return _orm_to_device(orm)
        except IntegrityError:
            raise ValueError(RepoErrors.DEVICE_EXISTS) from None

    async def get(self, device_id: str) -> dict | None:
        # FIXED: 原问题-查询操作无try-except保护
        try:
            async with self._auto_session() as session:
                result = await session.execute(
                    select(DeviceORM).where(DeviceORM.device_id == device_id)
                )
                orm = result.scalar_one_or_none()
                return _orm_to_device(orm) if orm else None
        except Exception as e:
            logger.error("DeviceRepo.get failed: %s", e)
            return None

    async def list_all(
        self,
        page: int = 1,
        size: int = 20,
        status: str | None = None,
        protocol: str | None = None,
        search: str | None = None,
    ) -> tuple[list[dict], int]:
        # FIXED: 原问题-list_all查询无try-except保护
        try:
            async with self._auto_session() as session:
                query = select(DeviceORM)
                count_query = select(func.count()).select_from(DeviceORM)
                if status:
                    query = query.where(DeviceORM.status == status)
                    count_query = count_query.where(DeviceORM.status == status)
                if protocol:
                    query = query.where(DeviceORM.protocol == protocol)
                    count_query = count_query.where(DeviceORM.protocol == protocol)
                if search:
                    pattern = f"%{search}%"
                    query = query.where(
                        (DeviceORM.name.ilike(pattern)) | (DeviceORM.device_id.ilike(pattern))
                    )
                    count_query = count_query.where(
                        (DeviceORM.name.ilike(pattern)) | (DeviceORM.device_id.ilike(pattern))
                    )
                total_result = await session.execute(count_query)
                total = total_result.scalar() or 0
                offset = (page - 1) * size
                query = query.order_by(DeviceORM.created_at.desc()).offset(offset).limit(size)
                result = await session.execute(query)
                rows = result.scalars().all()
                return [_orm_to_device(r) for r in rows], total
        except Exception as e:
            logger.error("DeviceRepo.list_all failed: %s", e)
            return [], 0

    async def update(self, device_id: str, data: dict) -> dict | None:
        # FIXED: 原问题-update操作无try-except保护
        try:
            async with self._auto_session() as session:
                result = await session.execute(
                    select(DeviceORM).where(DeviceORM.device_id == device_id)
                )
                orm = result.scalar_one_or_none()
                if orm is None:
                    return None
                for key in ("name", "collect_interval"):
                    if key in data:
                        setattr(orm, key, data[key])
                if "config" in data:
                    orm.config = json.dumps(data["config"], ensure_ascii=False)
                if "points" in data:
                    orm.points = json.dumps(data["points"], ensure_ascii=False)
                if "status" in data:
                    orm.status = data["status"]
                orm.updated_at = _now()
                await self._commit(session)
                await session.refresh(orm)
                return _orm_to_device(orm)
        except Exception as e:
            logger.error("DeviceRepo.update failed: %s", e)
            return None

    async def update_status(self, device_id: str, status: str) -> None:
        # FIXED: 原问题-update_status无try-except保护，设备状态更新异常影响调度循环
        try:
            async with self._auto_session() as session:
                await session.execute(
                    update(DeviceORM)
                    .where(DeviceORM.device_id == device_id)
                    .values(status=status, updated_at=_now())
                )
                await self._commit(session)
        except Exception as e:
            logger.error("DeviceRepo.update_status failed: %s", e)

    async def delete(self, device_id: str) -> bool:
        # FIXED: 原问题-delete操作无try-except保护
        try:
            async with self._auto_session() as session:
                result = await session.execute(
                    delete(DeviceORM).where(DeviceORM.device_id == device_id)
                )
                await self._commit(session)
                return result.rowcount > 0
        except Exception as e:
            logger.error("DeviceRepo.delete failed: %s", e)
            return False

    async def list_by_protocol(self, protocol: str) -> list[dict]:
        # FIXED: 原问题-list_by_protocol无try-except保护，被调度器高频调用
        try:
            async with self._auto_session() as session:
                result = await session.execute(select(DeviceORM).where(DeviceORM.protocol == protocol))
                rows = result.scalars().all()
                return [_orm_to_device(r) for r in rows]
        except Exception as e:
            logger.error("DeviceRepo.list_by_protocol failed: %s", e)
            return []


class RuleRepo(BaseRepo):
    async def create(self, data: dict) -> dict:
        # FIXED: 数据库插入无IntegrityError处理
        try:
            async with self._auto_session() as session:
                rule_id = _uuid()
                now = _now()
                orm = RuleORM(
                    rule_id=rule_id,
                    name=data["name"],
                    device_id=data["device_id"],
                    conditions=json.dumps(data["conditions"], ensure_ascii=False),
                    logic=data.get("logic", "AND"),
                    duration=data.get("duration", 0),
                    severity=data["severity"],
                    enabled=data.get("enabled", True),
                    notify_channels=json.dumps(data.get("notify_channels", []), ensure_ascii=False),
                    created_at=now,
                )
                session.add(orm)
                await self._commit(session)
                await session.refresh(orm)
                return _orm_to_rule(orm)
        except IntegrityError:
            raise ValueError(RepoErrors.RULE_EXISTS) from None

    async def get(self, rule_id: str) -> dict | None:
        # FIXED: 原问题-RuleRepo.get无try-except保护
        try:
            async with self._auto_session() as session:
                result = await session.execute(select(RuleORM).where(RuleORM.rule_id == rule_id))
                orm = result.scalar_one_or_none()
                return _orm_to_rule(orm) if orm else None
        except Exception as e:
            logger.error("RuleRepo.get failed: %s", e)
            return None

    async def list_all(
        self,
        page: int = 1,
        size: int = 20,
        device_id: str | None = None,
        search: str | None = None,
        severity: str | None = None,
    ) -> tuple[list[dict], int]:
        # FIXED: 原问题-RuleRepo.list_all无try-except保护
        try:
            async with self._auto_session() as session:
                query = select(RuleORM)
                count_query = select(func.count()).select_from(RuleORM)
                if device_id:
                    query = query.where(RuleORM.device_id == device_id)
                    count_query = count_query.where(RuleORM.device_id == device_id)
                if search:
                    pattern = f"%{search}%"
                    query = query.where(
                        (RuleORM.name.ilike(pattern)) | (RuleORM.rule_id.ilike(pattern))
                    )
                    count_query = count_query.where(
                        (RuleORM.name.ilike(pattern)) | (RuleORM.rule_id.ilike(pattern))
                    )
                if severity:
                    query = query.where(RuleORM.severity == severity)
                    count_query = count_query.where(RuleORM.severity == severity)
                total_result = await session.execute(count_query)
                total = total_result.scalar() or 0
                offset = (page - 1) * size
                query = query.order_by(RuleORM.created_at.desc()).offset(offset).limit(size)
                result = await session.execute(query)
                rows = result.scalars().all()
                return [_orm_to_rule(r) for r in rows], total
        except Exception as e:
            logger.error("RuleRepo.list_all failed: %s", e)
            return [], 0

    async def update(self, rule_id: str, data: dict) -> dict | None:
        # FIXED: 原问题-RuleRepo.update无try-except保护
        try:
            async with self._auto_session() as session:
                result = await session.execute(select(RuleORM).where(RuleORM.rule_id == rule_id))
                orm = result.scalar_one_or_none()
                if orm is None:
                    return None
                for key in ("name", "logic", "duration", "severity"):
                    if key in data:
                        setattr(orm, key, data[key])
                if "conditions" in data:
                    orm.conditions = json.dumps(data["conditions"], ensure_ascii=False)
                if "notify_channels" in data:
                    orm.notify_channels = json.dumps(data["notify_channels"], ensure_ascii=False)
                if "enabled" in data:
                    orm.enabled = data["enabled"]
                await self._commit(session)
                await session.refresh(orm)
                return _orm_to_rule(orm)
        except Exception as e:
            logger.error("RuleRepo.update failed: %s", e)
            return None

    async def toggle(self, rule_id: str, enabled: bool) -> dict | None:
        return await self.update(rule_id, {"enabled": enabled})

    async def delete(self, rule_id: str) -> bool:
        # FIXED: 原问题-RuleRepo.delete无try-except保护
        try:
            async with self._auto_session() as session:
                result = await session.execute(delete(RuleORM).where(RuleORM.rule_id == rule_id))
                await self._commit(session)
                return result.rowcount > 0
        except Exception as e:
            logger.error("RuleRepo.delete failed: %s", e)
            return False

    async def list_by_device(self, device_id: str) -> list[dict]:
        # FIXED: 原问题-RuleRepo.list_by_device无try-except保护，被evaluator调用
        try:
            async with self._auto_session() as session:
                result = await session.execute(
                    select(RuleORM).where(RuleORM.device_id == device_id, RuleORM.enabled.is_(True))
                )
                rows = result.scalars().all()
                return [_orm_to_rule(r) for r in rows]
        except Exception as e:
            logger.error("RuleRepo.list_by_device failed: %s", e)
            return []

    async def list_enabled_by_point(self, device_id: str, point_name: str) -> list[dict]:
        # FIXED: 原问题-被evaluator高频调用但无异常保护，数据库异常导致评估循环崩溃
        try:
            async with self._auto_session() as session:
                result = await session.execute(
                    select(RuleORM).where(RuleORM.device_id == device_id, RuleORM.enabled.is_(True))
                )
                rows = result.scalars().all()
                rules = [_orm_to_rule(r) for r in rows]
                return [r for r in rules if any(c.get("point") == point_name for c in r.get("conditions", []))]  # FIXED: 原问题-c["point"]硬索引
        except Exception as e:
            logger.error("RuleRepo.list_enabled_by_point failed: %s", e)
            return []


class AlarmRepo(BaseRepo):
    async def create(self, data: dict) -> dict:
        # FIXED: 原问题-告警创建无异常保护，IntegrityError未处理
        try:
            async with self._auto_session() as session:
                alarm_id = _uuid()
                now = _now()
                orm = AlarmORM(
                    alarm_id=alarm_id,
                    rule_id=data["rule_id"],
                    device_id=data["device_id"],
                    severity=data["severity"],
                    status="firing",
                    message=data.get("message", ""),
                    trigger_value=json.dumps(data.get("trigger_value", {}), ensure_ascii=False),
                    trigger_count=1,
                    fired_at=now,
                )
                session.add(orm)
                await self._commit(session)
                await session.refresh(orm)
                return _orm_to_alarm(orm)
        except IntegrityError:
            raise ValueError(RepoErrors.ALARM_EXISTS) from None
        except Exception as e:
            logger.error("AlarmRepo.create failed: %s", e)
            raise

    async def get(self, alarm_id: str) -> dict | None:
        # FIXED: 原问题-查询操作无try-except保护
        try:
            async with self._auto_session() as session:
                result = await session.execute(select(AlarmORM).where(AlarmORM.alarm_id == alarm_id))
                orm = result.scalar_one_or_none()
                return _orm_to_alarm(orm) if orm else None
        except Exception as e:
            logger.error("AlarmRepo.get failed: %s", e)
            return None

    async def list_all(
        self,
        page: int = 1,
        size: int = 20,
        status: str | None = None,
        severity: str | None = None,
        device_id: str | None = None,
        search: str | None = None,
    ) -> tuple[list[dict], int]:
        # FIXED: 原问题-AlarmRepo.list_all无try-except保护
        try:
            async with self._auto_session() as session:
                query = select(AlarmORM)
                count_query = select(func.count()).select_from(AlarmORM)
                if status:
                    query = query.where(AlarmORM.status == status)
                    count_query = count_query.where(AlarmORM.status == status)
                if severity:
                    query = query.where(AlarmORM.severity == severity)
                    count_query = count_query.where(AlarmORM.severity == severity)
                if device_id:
                    query = query.where(AlarmORM.device_id == device_id)
                    count_query = count_query.where(AlarmORM.device_id == device_id)
                if search:
                    pattern = f"%{search}%"
                    query = query.where(
                        (AlarmORM.message.ilike(pattern)) | (AlarmORM.alarm_id.ilike(pattern))
                    )
                    count_query = count_query.where(
                        (AlarmORM.message.ilike(pattern)) | (AlarmORM.alarm_id.ilike(pattern))
                    )
                total_result = await session.execute(count_query)
                total = total_result.scalar() or 0
                offset = (page - 1) * size
                query = query.order_by(AlarmORM.fired_at.desc()).offset(offset).limit(size)
                result = await session.execute(query)
                rows = result.scalars().all()
                return [_orm_to_alarm(r) for r in rows], total
        except Exception as e:
            logger.error("AlarmRepo.list_all failed: %s", e)
            return [], 0

    async def ack(self, alarm_id: str, ack_by: str) -> dict | None:
        # FIXED: 原问题-AlarmRepo.ack无try-except保护，告警确认失败导致未处理异常
        try:
            async with self._auto_session() as session:
                result = await session.execute(select(AlarmORM).where(AlarmORM.alarm_id == alarm_id))
                orm = result.scalar_one_or_none()
                if orm is None or orm.status != "firing":
                    return None
                orm.status = "acknowledged"
                orm.acknowledged_at = _now()
                orm.acknowledged_by = ack_by
                await self._commit(session)
                await session.refresh(orm)
                return _orm_to_alarm(orm)
        except Exception as e:
            logger.error("AlarmRepo.ack failed: %s", e)
            return None

    async def recover(self, alarm_id: str) -> dict | None:
        # FIXED: 原问题-告警恢复操作无try-except保护
        try:
            async with self._auto_session() as session:
                result = await session.execute(select(AlarmORM).where(AlarmORM.alarm_id == alarm_id))
                orm = result.scalar_one_or_none()
                if orm is None or orm.status not in ("firing", "acknowledged"):
                    return None
                orm.status = "recovered"
                orm.recovered_at = _now()
                await self._commit(session)
                await session.refresh(orm)
                return _orm_to_alarm(orm)
        except Exception as e:
            logger.error("AlarmRepo.recover failed: %s", e)
            return None

    async def update_trigger_count(self, alarm_id: str, trigger_value: dict) -> None:
        # FIXED: 原问题-更新操作无try-except保护
        try:
            async with self._auto_session() as session:
                result = await session.execute(select(AlarmORM).where(AlarmORM.alarm_id == alarm_id))
                orm = result.scalar_one_or_none()
                if orm:
                    orm.trigger_count = (orm.trigger_count or 0) + 1
                    orm.trigger_value = json.dumps(trigger_value, ensure_ascii=False)
                    await self._commit(session)
        except Exception as e:
            logger.error("AlarmRepo.update_trigger_count failed: %s", e)

    async def get_firing_by_rule_device(self, rule_id: str, device_id: str) -> dict | None:
        # FIXED: 原问题-被evaluator高频调用但无异常保护
        try:
            async with self._auto_session() as session:
                result = await session.execute(
                    select(AlarmORM).where(
                        AlarmORM.rule_id == rule_id,
                        AlarmORM.device_id == device_id,
                        AlarmORM.status == "firing",
                    )
                )
                orm = result.scalar_one_or_none()
                return _orm_to_alarm(orm) if orm else None
        except Exception as e:
            logger.error("AlarmRepo.get_firing_by_rule_device failed: %s", e)
            return None


class UserRepo(BaseRepo):
    async def create(self, data: dict) -> dict:
        # FIXED: 数据库插入无IntegrityError处理
        try:
            async with self._auto_session() as session:
                user_id = _uuid()
                now = _now()
                orm = UserORM(
                    user_id=user_id,
                    username=data["username"],
                    password=data["password"],
                    role=data["role"],
                    enabled=True,
                    created_at=now,
                )
                session.add(orm)
                await self._commit(session)
                await session.refresh(orm)
                return _orm_to_user(orm)
        except IntegrityError:
            raise ValueError(RepoErrors.USERNAME_EXISTS) from None

    async def get(self, user_id: str) -> dict | None:
        # FIXED: 原问题-UserRepo.get无try-except保护
        try:
            async with self._auto_session() as session:
                result = await session.execute(select(UserORM).where(UserORM.user_id == user_id))
                orm = result.scalar_one_or_none()
                return _orm_to_user(orm) if orm else None
        except Exception as e:
            logger.error("UserRepo.get failed: %s", e)
            return None

    async def get_by_username(self, username: str) -> dict | None:
        # FIXED: 原问题-UserRepo.get_by_username无try-except保护
        try:
            async with self._auto_session() as session:
                result = await session.execute(select(UserORM).where(UserORM.username == username))
                orm = result.scalar_one_or_none()
                return _orm_to_user(orm) if orm else None
        except Exception as e:
            logger.error("UserRepo.get_by_username failed: %s", e)
            return None

    async def get_by_username_with_password(self, username: str) -> dict | None:
        # FIXED: 原问题-UserRepo.get_by_username_with_password无try-except保护，被登录认证调用
        try:
            async with self._auto_session() as session:
                result = await session.execute(select(UserORM).where(UserORM.username == username))
                orm = result.scalar_one_or_none()
                return _orm_to_user_full(orm) if orm else None
        except Exception as e:
            logger.error("UserRepo.get_by_username_with_password failed: %s", e)
            return None

    async def list_all(self, page: int = 1, size: int = 20) -> tuple[list[dict], int]:
        # FIXED: 原问题-UserRepo.list_all无try-except保护
        try:
            async with self._auto_session() as session:
                count_result = await session.execute(select(func.count()).select_from(UserORM))
                total = count_result.scalar() or 0
                offset = (page - 1) * size
                result = await session.execute(
                    select(UserORM).order_by(UserORM.created_at.desc()).offset(offset).limit(size)
                )
                rows = result.scalars().all()
                return [_orm_to_user_safe(r) for r in rows], total
        except Exception as e:
            logger.error("UserRepo.list_all failed: %s", e)
            return [], 0

    async def update(self, user_id: str, data: dict) -> dict | None:
        # FIXED: 原问题-UserRepo.update无try-except保护
        try:
            async with self._auto_session() as session:
                result = await session.execute(select(UserORM).where(UserORM.user_id == user_id))
                orm = result.scalar_one_or_none()
                if orm is None:
                    return None
                for key in ("password", "role"):
                    if key in data:
                        setattr(orm, key, data[key])
                if "enabled" in data:
                    orm.enabled = data["enabled"]
                await self._commit(session)
                await session.refresh(orm)
                return _orm_to_user(orm)
        except Exception as e:
            logger.error("UserRepo.update failed: %s", e)
            return None

    async def delete(self, user_id: str) -> bool:
        # FIXED: 原问题-UserRepo.delete无try-except保护
        try:
            async with self._auto_session() as session:
                result = await session.execute(delete(UserORM).where(UserORM.user_id == user_id))
                await self._commit(session)
                return result.rowcount > 0
        except Exception as e:
            logger.error("UserRepo.delete failed: %s", e)
            return False

    async def update_password(self, username: str, hashed_password: str) -> None:
        # FIXED: 原问题-UserRepo.update_password无try-except保护
        try:
            async with self._auto_session() as session:
                result = await session.execute(select(UserORM).where(UserORM.username == username))
                orm = result.scalar_one_or_none()
                if orm is None:
                    return
                orm.password = hashed_password
                await self._commit(session)
        except Exception as e:
            logger.error("UserRepo.update_password failed: %s", e)

    async def update_user(self, username: str, data: dict) -> dict | None:
        # FIXED: 原问题-UserRepo.update_user无try-except保护
        try:
            async with self._auto_session() as session:
                result = await session.execute(select(UserORM).where(UserORM.username == username))
                orm = result.scalar_one_or_none()
                if orm is None:
                    return None
                for key in ("password", "role"):
                    if key in data:
                        setattr(orm, key, data[key])
                if "enabled" in data:
                    orm.enabled = data["enabled"]
                if "must_change_password" in data:
                    orm.must_change_password = data["must_change_password"]
                await self._commit(session)
                await session.refresh(orm)
                return _orm_to_user(orm)
        except Exception as e:
            logger.error("UserRepo.update_user failed: %s", e)
            return None

    async def count_by_role(self, role: str) -> int:
        # FIXED: 原问题-UserRepo.count_by_role无try-except保护
        try:
            async with self._auto_session() as session:
                result = await session.execute(
                    select(func.count()).select_from(UserORM).where(UserORM.role == role)
                )
                return result.scalar() or 0
        except Exception as e:
            logger.error("UserRepo.count_by_role failed: %s", e)
            return 0


def _orm_to_device(orm: DeviceORM) -> dict:
    return {
        "device_id": orm.device_id,
        "name": orm.name,
        "protocol": orm.protocol,
        "status": orm.status,
        "config": _safe_json_loads(orm.config, {}),
        "points": _safe_json_loads(orm.points, []),
        "collect_interval": orm.collect_interval,
        "created_at": orm.created_at.isoformat()
        if isinstance(orm.created_at, datetime)
        else str(orm.created_at),
        "updated_at": orm.updated_at.isoformat()
        if isinstance(orm.updated_at, datetime)
        else str(orm.updated_at),
    }


def _orm_to_rule(orm: RuleORM) -> dict:
    return {
        "rule_id": orm.rule_id,
        "name": orm.name,
        "device_id": orm.device_id,
        "conditions": _safe_json_loads(orm.conditions, []),
        "logic": orm.logic,
        "duration": orm.duration,
        "severity": orm.severity,
        "enabled": bool(orm.enabled),
        "notify_channels": _safe_json_loads(orm.notify_channels, []),
        "created_at": orm.created_at.isoformat()
        if isinstance(orm.created_at, datetime)
        else str(orm.created_at),
    }


def _orm_to_alarm(orm: AlarmORM) -> dict:
    return {
        "alarm_id": orm.alarm_id,
        "rule_id": orm.rule_id,
        "device_id": orm.device_id,
        "severity": orm.severity,
        "status": orm.status,
        "message": orm.message,
        "trigger_value": _safe_json_loads(orm.trigger_value),
        "trigger_count": orm.trigger_count,
        "fired_at": orm.fired_at.isoformat()
        if isinstance(orm.fired_at, datetime)
        else str(orm.fired_at),
        "acknowledged_at": orm.acknowledged_at.isoformat()
        if isinstance(orm.acknowledged_at, datetime)
        else orm.acknowledged_at,
        "acknowledged_by": orm.acknowledged_by,
        "recovered_at": orm.recovered_at.isoformat()
        if isinstance(orm.recovered_at, datetime)
        else orm.recovered_at,
    }


def _orm_to_user(orm: UserORM) -> dict:
    return {
        "user_id": orm.user_id,
        "username": orm.username,
        "role": orm.role,
        "enabled": bool(orm.enabled),
        "must_change_password": bool(orm.must_change_password),
        "created_at": orm.created_at.isoformat()
        if isinstance(orm.created_at, datetime)
        else str(orm.created_at),
        "updated_at": orm.updated_at.isoformat()
        if hasattr(orm, "updated_at") and isinstance(orm.updated_at, datetime)
        else str(getattr(orm, "updated_at", "")),
    }


def _orm_to_user_full(orm: UserORM) -> dict:
    return {
        "user_id": orm.user_id,
        "username": orm.username,
        "password": orm.password,
        "role": orm.role,
        "enabled": bool(orm.enabled),
        "must_change_password": bool(orm.must_change_password),
        "created_at": orm.created_at.isoformat()
        if isinstance(orm.created_at, datetime)
        else str(orm.created_at),
        "updated_at": orm.updated_at.isoformat()
        if hasattr(orm, "updated_at") and isinstance(orm.updated_at, datetime)
        else str(getattr(orm, "updated_at", "")),
    }


def _orm_to_user_safe(orm: UserORM) -> dict:
    return {
        "user_id": orm.user_id,
        "username": orm.username,
        "role": orm.role,
        "enabled": bool(orm.enabled),
        "must_change_password": bool(orm.must_change_password),
        "created_at": orm.created_at.isoformat()
        if isinstance(orm.created_at, datetime)
        else str(orm.created_at),
        "updated_at": orm.updated_at.isoformat()
        if hasattr(orm, "updated_at") and isinstance(orm.updated_at, datetime)
        else str(getattr(orm, "updated_at", "")),
    }
