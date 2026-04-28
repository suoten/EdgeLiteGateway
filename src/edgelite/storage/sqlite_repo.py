"""ORM-based 仓储实现（SQLAlchemy 2.0 异步）"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Sequence

from sqlalchemy import select, func, update, delete
from sqlalchemy.ext.asyncio import AsyncSession

from edgelite.models.db import DeviceORM, RuleORM, AlarmORM, UserORM, AuditLogORM


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return uuid.uuid4().hex[:16]


class DeviceRepo:
    def __init__(self, session: AsyncSession, write_lock: asyncio.Lock | None = None):
        self.session = session
        self._write_lock = write_lock

    async def _commit(self) -> None:
        if self._write_lock:
            async with self._write_lock:
                await self.session.commit()
        else:
            await self.session.commit()

    async def create(self, data: dict) -> dict:
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
        self.session.add(orm)
        await self._commit()
        await self.session.refresh(orm)
        return _orm_to_device(orm)

    async def get(self, device_id: str) -> dict | None:
        result = await self.session.execute(
            select(DeviceORM).where(DeviceORM.device_id == device_id)
        )
        orm = result.scalar_one_or_none()
        return _orm_to_device(orm) if orm else None

    async def list_all(
        self, page: int = 1, size: int = 20, status: str | None = None, protocol: str | None = None
    ) -> tuple[list[dict], int]:
        query = select(DeviceORM)
        count_query = select(func.count()).select_from(DeviceORM)
        if status:
            query = query.where(DeviceORM.status == status)
            count_query = count_query.where(DeviceORM.status == status)
        if protocol:
            query = query.where(DeviceORM.protocol == protocol)
            count_query = count_query.where(DeviceORM.protocol == protocol)
        total_result = await self.session.execute(count_query)
        total = total_result.scalar() or 0
        offset = (page - 1) * size
        query = query.order_by(DeviceORM.created_at.desc()).offset(offset).limit(size)
        result = await self.session.execute(query)
        rows = result.scalars().all()
        return [_orm_to_device(r) for r in rows], total

    async def update(self, device_id: str, data: dict) -> dict | None:
        result = await self.session.execute(
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
        await self._commit()
        await self.session.refresh(orm)
        return _orm_to_device(orm)

    async def update_status(self, device_id: str, status: str) -> None:
        await self.session.execute(
            update(DeviceORM).where(DeviceORM.device_id == device_id).values(status=status, updated_at=_now())
        )
        await self._commit()

    async def delete(self, device_id: str) -> bool:
        result = await self.session.execute(
            delete(DeviceORM).where(DeviceORM.device_id == device_id)
        )
        await self._commit()
        return result.rowcount > 0

    async def list_by_protocol(self, protocol: str) -> list[dict]:
        result = await self.session.execute(
            select(DeviceORM).where(DeviceORM.protocol == protocol)
        )
        rows = result.scalars().all()
        return [_orm_to_device(r) for r in rows]


class RuleRepo:
    def __init__(self, session: AsyncSession, write_lock: asyncio.Lock | None = None):
        self.session = session
        self._write_lock = write_lock

    async def _commit(self) -> None:
        if self._write_lock:
            async with self._write_lock:
                await self.session.commit()
        else:
            await self.session.commit()

    async def create(self, data: dict) -> dict:
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
        self.session.add(orm)
        await self._commit()
        await self.session.refresh(orm)
        return _orm_to_rule(orm)

    async def get(self, rule_id: str) -> dict | None:
        result = await self.session.execute(
            select(RuleORM).where(RuleORM.rule_id == rule_id)
        )
        orm = result.scalar_one_or_none()
        return _orm_to_rule(orm) if orm else None

    async def list_all(self, page: int = 1, size: int = 20, device_id: str | None = None) -> tuple[list[dict], int]:
        query = select(RuleORM)
        count_query = select(func.count()).select_from(RuleORM)
        if device_id:
            query = query.where(RuleORM.device_id == device_id)
            count_query = count_query.where(RuleORM.device_id == device_id)
        total_result = await self.session.execute(count_query)
        total = total_result.scalar() or 0
        offset = (page - 1) * size
        query = query.order_by(RuleORM.created_at.desc()).offset(offset).limit(size)
        result = await self.session.execute(query)
        rows = result.scalars().all()
        return [_orm_to_rule(r) for r in rows], total

    async def update(self, rule_id: str, data: dict) -> dict | None:
        result = await self.session.execute(
            select(RuleORM).where(RuleORM.rule_id == rule_id)
        )
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
        await self._commit()
        await self.session.refresh(orm)
        return _orm_to_rule(orm)

    async def toggle(self, rule_id: str, enabled: bool) -> dict | None:
        return await self.update(rule_id, {"enabled": enabled})

    async def delete(self, rule_id: str) -> bool:
        result = await self.session.execute(
            delete(RuleORM).where(RuleORM.rule_id == rule_id)
        )
        await self._commit()
        return result.rowcount > 0

    async def list_by_device(self, device_id: str) -> list[dict]:
        result = await self.session.execute(
            select(RuleORM).where(RuleORM.device_id == device_id, RuleORM.enabled.is_(True))
        )
        rows = result.scalars().all()
        return [_orm_to_rule(r) for r in rows]

    async def list_enabled_by_point(self, device_id: str, point_name: str) -> list[dict]:
        result = await self.session.execute(
            select(RuleORM).where(RuleORM.device_id == device_id, RuleORM.enabled.is_(True))
        )
        rows = result.scalars().all()
        rules = [_orm_to_rule(r) for r in rows]
        return [r for r in rules if any(c["point"] == point_name for c in r["conditions"])]


class AlarmRepo:
    def __init__(self, session: AsyncSession, write_lock: asyncio.Lock | None = None):
        self.session = session
        self._write_lock = write_lock

    async def _commit(self) -> None:
        if self._write_lock:
            async with self._write_lock:
                await self.session.commit()
        else:
            await self.session.commit()

    async def create(self, data: dict) -> dict:
        alarm_id = _uuid()
        now = _now()
        orm = AlarmORM(
            alarm_id=alarm_id,
            rule_id=data["rule_id"],
            device_id=data["device_id"],
            severity=data["severity"],
            status="firing",
            trigger_value=json.dumps(data.get("trigger_value", {}), ensure_ascii=False),
            trigger_count=1,
            fired_at=now,
        )
        self.session.add(orm)
        await self._commit()
        await self.session.refresh(orm)
        return _orm_to_alarm(orm)

    async def get(self, alarm_id: str) -> dict | None:
        result = await self.session.execute(
            select(AlarmORM).where(AlarmORM.alarm_id == alarm_id)
        )
        orm = result.scalar_one_or_none()
        return _orm_to_alarm(orm) if orm else None

    async def list_all(
        self, page: int = 1, size: int = 20, status: str | None = None,
        severity: str | None = None, device_id: str | None = None,
    ) -> tuple[list[dict], int]:
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
        total_result = await self.session.execute(count_query)
        total = total_result.scalar() or 0
        offset = (page - 1) * size
        query = query.order_by(AlarmORM.fired_at.desc()).offset(offset).limit(size)
        result = await self.session.execute(query)
        rows = result.scalars().all()
        return [_orm_to_alarm(r) for r in rows], total

    async def ack(self, alarm_id: str, ack_by: str) -> dict | None:
        result = await self.session.execute(
            select(AlarmORM).where(AlarmORM.alarm_id == alarm_id)
        )
        orm = result.scalar_one_or_none()
        if orm is None:
            return None
        orm.status = "acknowledged"
        orm.acknowledged_at = _now()
        orm.acknowledged_by = ack_by
        await self._commit()
        await self.session.refresh(orm)
        return _orm_to_alarm(orm)

    async def recover(self, alarm_id: str) -> dict | None:
        result = await self.session.execute(
            select(AlarmORM).where(AlarmORM.alarm_id == alarm_id)
        )
        orm = result.scalar_one_or_none()
        if orm is None:
            return None
        orm.status = "recovered"
        orm.recovered_at = _now()
        await self._commit()
        await self.session.refresh(orm)
        return _orm_to_alarm(orm)

    async def update_trigger_count(self, alarm_id: str, trigger_value: dict) -> None:
        result = await self.session.execute(
            select(AlarmORM).where(AlarmORM.alarm_id == alarm_id)
        )
        orm = result.scalar_one_or_none()
        if orm:
            orm.trigger_count = AlarmORM.trigger_count + 1
            orm.trigger_value = json.dumps(trigger_value, ensure_ascii=False)
            await self._commit()

    async def get_firing_by_rule_device(self, rule_id: str, device_id: str) -> dict | None:
        result = await self.session.execute(
            select(AlarmORM).where(
                AlarmORM.rule_id == rule_id, AlarmORM.device_id == device_id, AlarmORM.status == "firing",
            )
        )
        orm = result.scalar_one_or_none()
        return _orm_to_alarm(orm) if orm else None


class UserRepo:
    def __init__(self, session: AsyncSession, write_lock: asyncio.Lock | None = None):
        self.session = session
        self._write_lock = write_lock

    async def _commit(self) -> None:
        if self._write_lock:
            async with self._write_lock:
                await self.session.commit()
        else:
            await self.session.commit()

    async def create(self, data: dict) -> dict:
        user_id = _uuid()
        now = _now()
        orm = UserORM(
            user_id=user_id, username=data["username"], password=data["password"],
            role=data["role"], enabled=True, created_at=now,
        )
        self.session.add(orm)
        await self._commit()
        await self.session.refresh(orm)
        return _orm_to_user(orm)

    async def get(self, user_id: str) -> dict | None:
        result = await self.session.execute(
            select(UserORM).where(UserORM.user_id == user_id)
        )
        orm = result.scalar_one_or_none()
        return _orm_to_user(orm) if orm else None

    async def get_by_username(self, username: str) -> dict | None:
        result = await self.session.execute(
            select(UserORM).where(UserORM.username == username)
        )
        orm = result.scalar_one_or_none()
        return _orm_to_user(orm) if orm else None

    async def list_all(self, page: int = 1, size: int = 20) -> tuple[list[dict], int]:
        count_result = await self.session.execute(select(func.count()).select_from(UserORM))
        total = count_result.scalar() or 0
        offset = (page - 1) * size
        result = await self.session.execute(
            select(UserORM).order_by(UserORM.created_at.desc()).offset(offset).limit(size)
        )
        rows = result.scalars().all()
        return [_orm_to_user(r) for r in rows], total

    async def update(self, user_id: str, data: dict) -> dict | None:
        result = await self.session.execute(
            select(UserORM).where(UserORM.user_id == user_id)
        )
        orm = result.scalar_one_or_none()
        if orm is None:
            return None
        for key in ("password", "role"):
            if key in data:
                setattr(orm, key, data[key])
        if "enabled" in data:
            orm.enabled = data["enabled"]
        await self._commit()
        await self.session.refresh(orm)
        return _orm_to_user(orm)

    async def delete(self, user_id: str) -> bool:
        result = await self.session.execute(
            delete(UserORM).where(UserORM.user_id == user_id)
        )
        await self._commit()
        return result.rowcount > 0


class AuditRepo:
    def __init__(self, session: AsyncSession, write_lock: asyncio.Lock | None = None):
        self.session = session
        self._write_lock = write_lock

    async def _commit(self) -> None:
        if self._write_lock:
            async with self._write_lock:
                await self.session.commit()
        else:
            await self.session.commit()

    async def create(self, data: dict) -> None:
        orm = AuditLogORM(
            user_id=data["user_id"], username=data.get("username"),
            action=data["action"], resource_type=data.get("resource_type"),
            resource_id=data.get("resource_id"), ip_address=data.get("ip_address"),
            user_agent=data.get("user_agent"),
            details=json.dumps(data.get("details"), ensure_ascii=False) if data.get("details") else None,
            status=data.get("status", "success"), error_message=data.get("error_message"),
        )
        self.session.add(orm)
        await self._commit()


def _orm_to_device(orm: DeviceORM) -> dict:
    return {
        "device_id": orm.device_id, "name": orm.name, "protocol": orm.protocol,
        "status": orm.status,
        "config": json.loads(orm.config) if isinstance(orm.config, str) else orm.config,
        "points": json.loads(orm.points) if isinstance(orm.points, str) else orm.points,
        "collect_interval": orm.collect_interval,
        "created_at": orm.created_at.isoformat() if isinstance(orm.created_at, datetime) else str(orm.created_at),
        "updated_at": orm.updated_at.isoformat() if isinstance(orm.updated_at, datetime) else str(orm.updated_at),
    }


def _orm_to_rule(orm: RuleORM) -> dict:
    return {
        "rule_id": orm.rule_id, "name": orm.name, "device_id": orm.device_id,
        "conditions": json.loads(orm.conditions) if isinstance(orm.conditions, str) else orm.conditions,
        "logic": orm.logic, "duration": orm.duration, "severity": orm.severity,
        "enabled": bool(orm.enabled),
        "notify_channels": json.loads(orm.notify_channels) if isinstance(orm.notify_channels, str) else orm.notify_channels,
        "created_at": orm.created_at.isoformat() if isinstance(orm.created_at, datetime) else str(orm.created_at),
    }


def _orm_to_alarm(orm: AlarmORM) -> dict:
    return {
        "alarm_id": orm.alarm_id, "rule_id": orm.rule_id, "device_id": orm.device_id,
        "severity": orm.severity, "status": orm.status,
        "trigger_value": json.loads(orm.trigger_value) if isinstance(orm.trigger_value, str) else orm.trigger_value,
        "trigger_count": orm.trigger_count,
        "fired_at": orm.fired_at.isoformat() if isinstance(orm.fired_at, datetime) else str(orm.fired_at),
        "acknowledged_at": orm.acknowledged_at.isoformat() if isinstance(orm.acknowledged_at, datetime) else orm.acknowledged_at,
        "acknowledged_by": orm.acknowledged_by,
        "recovered_at": orm.recovered_at.isoformat() if isinstance(orm.recovered_at, datetime) else orm.recovered_at,
    }


def _orm_to_user(orm: UserORM) -> dict:
    return {
        "user_id": orm.user_id, "username": orm.username, "password": orm.password,
        "role": orm.role, "enabled": bool(orm.enabled),
        "created_at": orm.created_at.isoformat() if isinstance(orm.created_at, datetime) else str(orm.created_at),
    }
