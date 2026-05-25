"""SQLAlchemy ORM 数据库模型"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Boolean, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(UTC)


class DeviceORM(Base):
    __tablename__ = "devices"

    device_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    protocol: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="offline")
    config: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    points: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    collect_interval: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class RuleORM(Base):
    __tablename__ = "rules"

    rule_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    device_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    conditions: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    logic: Mapped[str] = mapped_column(String(8), nullable=False, default="AND")
    duration: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    notify_channels: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)


class AlarmORM(Base):
    __tablename__ = "alarms"

    alarm_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    rule_id: Mapped[str] = mapped_column(String(16), nullable=False)
    device_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="firing")
    message: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    trigger_value: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    trigger_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    fired_at: Mapped[datetime] = mapped_column(default=_utcnow)
    acknowledged_at: Mapped[datetime | None] = mapped_column(nullable=True)
    acknowledged_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    recovered_at: Mapped[datetime | None] = mapped_column(nullable=True)

    rule_type: Mapped[str] = mapped_column(String(32), nullable=False, default="threshold")

    __table_args__ = (
        Index("idx_alarms_status", "status"),
        Index("idx_alarms_device", "device_id"),
    )


class UserORM(Base):
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    username: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    password: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(nullable=True)


class CacheQueueORM(Base):
    __tablename__ = "cache_queue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    measurement: Mapped[str] = mapped_column(String(128), nullable=False)
    tags: Mapped[str] = mapped_column(Text, nullable=False)
    fields: Mapped[str] = mapped_column(Text, nullable=False)
    timestamp: Mapped[str] = mapped_column(Text, nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)

    __table_args__ = (
        Index("idx_cache_queue_status", "status"),
    )
