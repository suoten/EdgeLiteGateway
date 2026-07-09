"""SQLAlchemy ORM 数据库模型"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class StaleDataError(Exception):
    """FIXED-P0: 乐观锁冲突异常，并发更新时version不匹配抛出"""
    pass


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
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)  # FIXED-P0: 乐观锁版本号，防止并发更新丢失

    __table_args__ = (
        Index("idx_devices_created_by", "created_by"),
        CheckConstraint("collect_interval > 0", name="ck_devices_collect_interval_positive"),
        CheckConstraint("status IN ('online', 'offline', 'error', 'unknown')", name="ck_devices_status_valid"),
        CheckConstraint(  # FIXED-PROTOCOL: unified protocol set matching constants.VALID_DEVICE_PROTOCOLS
            "protocol IN ("
            "'modbus_tcp', 'modbus_rtu', 'simulator', 'mqtt_client', 'http_webhook', "
            "'opc_ua', 'siemens_s7', 'mitsubishi_mc', 'omron_fins', 'allen_bradley', "
            "'opc_da', 'onvif', 'video_ai', 'modbus_slave'"
            ")",
            name="ck_devices_protocol_valid",
        ),
    )


class RuleORM(Base):
    __tablename__ = "rules"

    rule_id: Mapped[str] = mapped_column(String(64), primary_key=True)  # FIXED(P1): 原问题-String(16)截断UUID/雪花ID; 修复-统一String(64)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    device_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    conditions: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    logic: Mapped[str] = mapped_column(String(8), nullable=False, default="AND")
    duration: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    notify_channels: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    # SEC-FIX: 新增 script/rule_type 列，使 evaluator 读取的脚本/规则类型可持久化
    script: Mapped[str] = mapped_column(Text, nullable=False, default="")
    rule_type: Mapped[str] = mapped_column(String(16), nullable=False, default="threshold")
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)  # FIXED-P2: 原问题-RuleORM缺失updated_at字段，与DeviceORM不一致，无法追踪规则最后修改时间
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)  # FIXED-P0: 乐观锁版本号，防止并发更新丢失

    __table_args__ = (
        # FIXED(P1): 原问题-RuleORM常用过滤字段device_id/enabled/severity/created_by无索引，规则查询全表扫描;
        # 修复-为常用过滤字段添加索引
        Index("idx_rules_device_id", "device_id"),
        Index("idx_rules_enabled", "enabled"),
        Index("idx_rules_severity", "severity"),
        Index("idx_rules_created_by", "created_by"),
        # FIXED-P1: 数据库层约束，防止非法值写入
        CheckConstraint("logic IN ('AND', 'OR', 'NOT')", name="ck_rules_logic_valid"),
        CheckConstraint("severity IN ('critical', 'major', 'warning', 'minor', 'info')", name="ck_rules_severity_valid"),
        CheckConstraint("duration >= 0", name="ck_rules_duration_non_negative"),
        CheckConstraint(  # SEC-FIX: 约束 rule_type 取值范围，与 AlarmORM/RuleTemplateORM/Pydantic 对齐
            "rule_type IN ('threshold', 'ai_inference', 'script')",
            name="ck_rules_rule_type_valid",
        ),
    )


# SEC-FIX-RULE-VERSION: API 规则版本快照表，支持误删/恶意修改后回滚。
# 参考 drivers/rule_store.py 的 rule_versions 表设计，为主库 rules 表提供版本历史。
class RuleVersionORM(Base):
    """规则版本快照表，记录每次规则变更前的快照，支持回滚到任意历史版本。"""

    __tablename__ = "rule_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[str] = mapped_column(Text, nullable=False)  # JSON 序列化的规则快照
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)  # SHA256
    change_summary: Mapped[str | None] = mapped_column(String(256), nullable=True, default=None)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)

    __table_args__ = (
        UniqueConstraint("rule_id", "version", name="uq_rule_version"),
        Index("idx_rule_versions_rule", "rule_id", "version"),
    )


class AlarmSilenceORM(Base):
    """FIXED-ALARM-SILENCE: Moved from independent alarm_silence.db to main DB.

    Tracks alarm silence / maintenance windows. Uses main DB session and write_lock
    so all operations are protected by the same transaction isolation as other entities.
    """

    __tablename__ = "alarm_silences"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    device_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("devices.device_id", ondelete="CASCADE"), nullable=True, default=None)  # FIXED-P1: 原问题-default=""且nullable=False，空字符串无意义；改为nullable允许全局静默; FIXED-P0: 添加外键约束，设备删除时级联删除静默规则
    rule_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("rules.rule_id", ondelete="SET NULL"), nullable=True, default=None)  # FIXED(P1): 原问题-String(16)截断UUID/雪花ID; 修复-统一String(64); FIXED-P1: 原问题-default=""且nullable=False，空字符串无意义；改为nullable允许规则级静默; FIXED-P0: 添加外键约束，规则删除时置NULL保留静默窗口
    start_time: Mapped[datetime] = mapped_column(nullable=False)
    end_time: Mapped[datetime] = mapped_column(nullable=False)
    reason: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    operator: Mapped[str] = mapped_column(String(64), nullable=False, default="system")
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)

    __table_args__ = (
        Index("idx_alarm_silences_device", "device_id"),
        Index("idx_alarm_silences_rule", "rule_id"),
        Index("idx_alarm_silences_window", "start_time", "end_time"),
    )


class AlarmORM(Base):
    __tablename__ = "alarms"

    alarm_id: Mapped[str] = mapped_column(String(64), primary_key=True)  # FIXED(P1): 原问题-String(16)截断UUID/雪花ID; 修复-统一String(64)
    rule_id: Mapped[str] = mapped_column(String(64), ForeignKey("rules.rule_id", ondelete="CASCADE"), nullable=False)  # FIXED(P1): 原问题-String(16)截断UUID/雪花ID; 修复-统一String(64); FIXED-P0: 添加外键约束，规则删除时级联删除告警
    device_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("devices.device_id", ondelete="CASCADE"), nullable=True)  # FIXED-P0: 添加外键约束，设备删除时级联删除告警
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
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)  # FIXED-P1: 原问题-AlarmORM无version列，并发确认/恢复可覆盖

    __table_args__ = (
        Index("idx_alarms_status", "status"),
        Index("idx_alarms_device", "device_id"),
        Index("idx_alarms_severity", "severity"),  # FIXED-P1: 原问题-severity为常用过滤字段但无索引，告警列表查询全表扫描
        Index("idx_alarms_fired_at", "fired_at"),  # FIXED-P2: 原问题-按时间排序/范围查询无索引
        CheckConstraint("severity IN ('critical', 'major', 'warning', 'minor', 'info')", name="ck_alarms_severity_valid"),  # FIXED-P1: 原问题-AlarmORM无CheckConstraint，severity/status/rule_type可写入任意值
        CheckConstraint("status IN ('firing', 'acknowledged', 'recovered')", name="ck_alarms_status_valid"),
        CheckConstraint("rule_type IN ('threshold', 'ai_inference', 'script')", name="ck_alarms_rule_type_valid"),  # FIXED-P0: 原问题-rule_type枚举与RuleORM不一致，script类型规则触发告警时IntegrityError；统一为('threshold','ai_inference','script')
    )


class UserORM(Base):
    __tablename__ = "users"

    # FIXED(P1): 原问题-user_id类型不一致(String(16) vs UserSessionORM/AuditLogORM的String(64));
    # 修复-统一为String(64)
    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    password: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    password_changed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)  # FIXED-P1: 原问题-UserORM无version列，并发修改可丢失更新

    __table_args__ = (
        CheckConstraint("role IN ('admin', 'operator', 'viewer')", name="ck_users_role_valid"),  # FIXED-P1: 原问题-UserORM无role CheckConstraint，可写入任意角色值
    )


class DeviceTemplateORM(Base):
    __tablename__ = "device_templates"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    protocol: Mapped[str] = mapped_column(String(32), nullable=False)
    config_template: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    point_templates: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __table_args__ = (
        CheckConstraint(  # FIXED-PROTOCOL: unified protocol set matching constants.VALID_DEVICE_PROTOCOLS
            "protocol IN ("
            "'modbus_tcp', 'modbus_rtu', 'simulator', 'mqtt_client', 'http_webhook', "
            "'opc_ua', 'siemens_s7', 'mitsubishi_mc', 'omron_fins', 'allen_bradley', "
            "'opc_da', 'onvif', 'video_ai', 'modbus_slave'"
            ")",
            name="ck_device_templates_protocol_valid",
        ),
    )


class RuleTemplateORM(Base):
    """FIXED-P2: 原问题-RuleTemplate纯内存存储，进程重启丢失；改为ORM持久化"""
    __tablename__ = "rule_templates"

    template_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    rule_type: Mapped[str] = mapped_column(String(32), nullable=False, default="threshold")
    default_conditions: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    default_severity: Mapped[str] = mapped_column(String(16), nullable=False, default="warning")
    default_duration: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    notify_channels: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        CheckConstraint(  # FIXED-P0: 原问题-severity枚举('info','warning','error','critical')与RuleORM/AlarmORM不一致，'error'非合法值且缺少'major'/'minor'，从模板创建规则时DB约束拒绝
            "default_severity IN ('critical', 'major', 'warning', 'minor', 'info')",
            name="ck_rule_templates_severity_valid",
        ),
        CheckConstraint(  # FIXED-P0: 原问题-rule_type枚举与AlarmORM/RuleORM不一致，统一为('threshold','ai_inference','script')与Pydantic对齐
            "rule_type IN ('threshold', 'ai_inference', 'script')",
            name="ck_rule_templates_type_valid",
        ),
    )


class DeviceGroupORM(Base):
    """FIXED-P2: 原问题-DeviceGroup纯内存存储，进程重启丢失；改为ORM持久化"""
    __tablename__ = "device_groups"

    group_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    parent_id: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    device_ids: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    tags: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        Index("idx_device_groups_parent", "parent_id"),
    )


class ResourceShareORM(Base):
    __tablename__ = "resource_shares"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(64), nullable=False)
    shared_with_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    permission_level: Mapped[str] = mapped_column(String(16), nullable=False, default="read")
    shared_by_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)

    __table_args__ = (
        Index("idx_resource_shares_lookup", "resource_type", "resource_id", "shared_with_user_id"),
        Index("idx_resource_shares_user", "shared_with_user_id"),
        UniqueConstraint("resource_type", "resource_id", "shared_with_user_id", name="uq_resource_shares_unique"),  # FIXED-P1: 原问题-同一资源可重复分享给同一用户
    )


class CacheQueueORM(Base):
    __tablename__ = "cache_queue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    measurement: Mapped[str] = mapped_column(String(128), nullable=False)
    tags: Mapped[str] = mapped_column(Text, nullable=False)
    fields: Mapped[str] = mapped_column(Text, nullable=False)
    timestamp: Mapped[float] = mapped_column(Float, nullable=False)  # FIXED-P2: 原问题-timestamp为Text类型，时间范围查询需CAST且无法正确排序；改为Float支持数值比较和索引
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)

    __table_args__ = (
        Index("idx_cache_queue_status", "status"),
        Index("idx_cache_queue_timestamp", "timestamp"),  # FIXED-P2: 原问题-按时间排序/清理无索引
    )


class RevokedTokenORM(Base):
    """FIXED-P0: SQLite-backed token revocation store for persistence across restarts."""
    __tablename__ = "revoked_tokens"

    jti: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[float] = mapped_column(nullable=False)
    revoked_at: Mapped[datetime] = mapped_column(default=_utcnow)

    __table_args__ = (
        Index("idx_revoked_tokens_expires", "expires_at"),
    )


class UserSessionORM(Base):
    """LP-09: 用户活跃会话持久化存储，重启后恢复并发登录控制。

    解决 session_manager 原 fail-open 策略：重启后内存状态丢失导致
    并发登录控制和 token 撤销机制双重失效。
    """
    __tablename__ = "user_sessions"

    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    jti: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[float] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)

    __table_args__ = (
        Index("idx_user_sessions_user", "user_id"),
        Index("idx_user_sessions_expires", "expires_at"),
    )


# FIXED-H03: Persistent login rate limiting storage for multi-worker deployments
class LoginAttemptORM(Base):
    """Track failed login attempts per IP for rate limiting."""
    __tablename__ = "login_attempts"

    ip: Mapped[str] = mapped_column(String(45), primary_key=True)  # IPv6 max length
    attempt_count: Mapped[int] = mapped_column(nullable=False, default=0)
    first_attempt_at: Mapped[float] = mapped_column(nullable=False)
    last_attempt_at: Mapped[float] = mapped_column(nullable=False)

    __table_args__ = (
        Index("idx_login_attempts_last", "last_attempt_at"),
    )


class AccountLockoutORM(Base):
    """Track account lockouts for username+IP combinations."""
    __tablename__ = "account_lockouts"

    lockout_key: Mapped[str] = mapped_column(String(128), primary_key=True)  # username:ip
    username: Mapped[str] = mapped_column(String(32), nullable=False)
    ip: Mapped[str] = mapped_column(String(45), nullable=False)
    lockout_until: Mapped[float] = mapped_column(nullable=False)
    fail_count: Mapped[int] = mapped_column(nullable=False, default=0)

    __table_args__ = (
        Index("idx_account_lockouts_user", "username"),
        Index("idx_account_lockouts_until", "lockout_until"),
    )


# FIXED-M03: Global login failure tracking and username-level lockout
class GlobalLoginFailureORM(Base):
    """Track global login failures for rate limiting across all IPs."""
    __tablename__ = "global_login_failures"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    timestamp: Mapped[float] = mapped_column(nullable=False)
    username: Mapped[str] = mapped_column(String(32), nullable=True)  # null for unknown username
    ip: Mapped[str] = mapped_column(String(45), nullable=True)

    __table_args__ = (
        Index("idx_global_failures_ts", "timestamp"),
    )


class GlobalAccountLockoutORM(Base):
    """Track global lockouts for usernames (regardless of IP)."""
    __tablename__ = "global_account_lockouts"

    username: Mapped[str] = mapped_column(String(32), primary_key=True)
    fail_count: Mapped[int] = mapped_column(nullable=False, default=0)
    first_attempt_at: Mapped[float] = mapped_column(nullable=False)
    last_attempt_at: Mapped[float] = mapped_column(nullable=False)
    locked_until: Mapped[float] = mapped_column(nullable=False, default=0)

    __table_args__ = (
        Index("idx_global_lockouts_until", "locked_until"),
    )


# FIXED-H01: Password reset request rate limiting
class PasswordResetAttemptORM(Base):
    """Track password reset requests per IP for rate limiting."""
    __tablename__ = "password_reset_attempts"

    ip: Mapped[str] = mapped_column(String(45), primary_key=True)  # IPv6 max length
    attempt_count: Mapped[int] = mapped_column(nullable=False, default=0)
    first_attempt_at: Mapped[float] = mapped_column(nullable=False)
    last_attempt_at: Mapped[float] = mapped_column(nullable=False)

    __table_args__ = (
        Index("idx_reset_attempts_last", "last_attempt_at"),
    )


class PasswordResetUserRateORM(Base):
    """Track password reset requests per username for rate limiting."""
    __tablename__ = "password_reset_user_rates"

    username: Mapped[str] = mapped_column(String(32), primary_key=True)
    attempt_count: Mapped[int] = mapped_column(nullable=False, default=0)
    first_attempt_at: Mapped[float] = mapped_column(nullable=False)
    last_attempt_at: Mapped[float] = mapped_column(nullable=False)

    __table_args__ = (
        Index("idx_reset_user_last", "last_attempt_at"),
    )


# FIXED-H03: Track used password reset tokens to prevent reuse
class UsedPasswordResetTokenORM(Base):
    """Track used password reset tokens (one-time use).

    When a password reset token is successfully used, it's recorded here.
    Subsequent attempts to use the same token will be rejected.
    """
    __tablename__ = "used_password_reset_tokens"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)  # SHA256 hash of token
    username: Mapped[str] = mapped_column(String(32), nullable=False)
    used_at: Mapped[float] = mapped_column(nullable=False)

    __table_args__ = (
        Index("idx_used_tokens_ts", "used_at"),
        Index("idx_used_tokens_user", "username"),
    )


# FIXED-H03: Track IP-level password reset usage attempts
class PasswordResetIPAttemptORM(Base):
    """Track password reset usage attempts per IP for rate limiting."""
    __tablename__ = "password_reset_ip_attempts"

    ip: Mapped[str] = mapped_column(String(45), primary_key=True)
    attempt_count: Mapped[int] = mapped_column(nullable=False, default=0)
    first_attempt_at: Mapped[float] = mapped_column(nullable=False)
    last_attempt_at: Mapped[float] = mapped_column(nullable=False)

    __table_args__ = (
        Index("idx_reset_ip_last", "last_attempt_at"),
    )


# FIXED-AUDIT-DB: Configuration change audit trail migrated from JSONL to main DB.
# Provides ACID guarantees, indexed queries, and integrates with backup/restore lifecycle.
class AuditLogORM(Base):
    """Audit log entry for configuration changes.

    Stored in the main database so it benefits from the same transaction isolation,
    write_lock protection, and backup/restore lifecycle as all other entities.
    """

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    audit_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)
    action: Mapped[str] = mapped_column(String(16), nullable=False)  # create, update, delete
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)  # device, rule, config, user
    resource_id: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    old_value: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    new_value: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    changes: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    ip_address: Mapped[str] = mapped_column(String(45), nullable=False, default="")
    user_agent: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    __table_args__ = (
        Index("idx_audit_timestamp", "timestamp"),
        Index("idx_audit_user", "user_id"),
        Index("idx_audit_resource", "resource_type", "resource_id"),
        Index("idx_audit_action", "action"),
    )
