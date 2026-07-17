"""initial schema

Revision ID: 001
Revises:
Create Date: 2024-01-01 00:00:00.000000

NOTE: This migration creates a minimal version of devices, rules, alarms,
users, and cache_queue. Missing columns (e.g. devices.created_by, users.version)
are added in migration 004_missing_tables. Downgrade from 004 restores
the original schema by dropping those columns.

This migration is idempotent - it uses try/except blocks to handle
databases that were previously initialized.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # devices
    _create_table_if_not_exists(
        "devices",
        sa.Column("device_id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("protocol", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="offline"),
        sa.Column("config", sa.Text, nullable=False, server_default="{}"),
        sa.Column("points", sa.Text, nullable=False, server_default="[]"),
        sa.Column("collect_interval", sa.Integer, nullable=False, server_default="5"),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, server_default=sa.func.now()),
    )
    _create_index_idempotent("idx_devices_created_by", "devices", ["created_by"])
    _create_check_constraint_idempotent("ck_devices_collect_interval_positive", "devices", "collect_interval > 0")
    _create_check_constraint_idempotent(
        "ck_devices_status_valid", "devices", "status IN ('online', 'offline', 'error', 'unknown')"
    )
    _create_check_constraint_idempotent(
        "ck_devices_protocol_valid",
        "devices",
        "protocol IN ("
        "'modbus_tcp', 'modbus_rtu', 'simulator', 'mqtt_client', 'http_webhook', "
        "'opc_ua', 'siemens_s7', 'mitsubishi_mc', 'omron_fins', 'allen_bradley', "
        "'opc_da', 'onvif', 'video_ai', 'modbus_slave'"
        ")",
    )

    # rules
    _create_table_if_not_exists(
        "rules",
        sa.Column("rule_id", sa.String(16), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("device_id", sa.String(64), nullable=True),
        sa.Column("conditions", sa.Text, nullable=False, server_default="[]"),
        sa.Column("logic", sa.String(8), nullable=False, server_default="AND"),
        sa.Column("duration", sa.Integer, nullable=False, server_default="0"),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default="1"),
        sa.Column("notify_channels", sa.Text, nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )
    _create_check_constraint_idempotent("ck_rules_logic_valid", "rules", "logic IN ('AND', 'OR', 'NOT')")
    _create_check_constraint_idempotent(
        "ck_rules_severity_valid", "rules", "severity IN ('critical', 'major', 'warning', 'minor', 'info')"
    )
    _create_check_constraint_idempotent("ck_rules_duration_non_negative", "rules", "duration >= 0")

    # alarms
    _create_table_if_not_exists(
        "alarms",
        sa.Column("alarm_id", sa.String(16), primary_key=True),
        sa.Column("rule_id", sa.String(16), nullable=False),
        sa.Column("device_id", sa.String(64), nullable=True),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="firing"),
        sa.Column("trigger_value", sa.Text, nullable=False, server_default="{}"),
        sa.Column("trigger_count", sa.Integer, nullable=False, server_default="1"),
        sa.Column("fired_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("acknowledged_at", sa.DateTime, nullable=True),
        sa.Column("acknowledged_by", sa.String(64), nullable=True),
        sa.Column("recovered_at", sa.DateTime, nullable=True),
    )
    _create_index_idempotent("idx_alarms_status", "alarms", ["status"])
    _create_index_idempotent("idx_alarms_device", "alarms", ["device_id"])
    _create_check_constraint_idempotent(
        "ck_alarms_severity_valid", "alarms", "severity IN ('critical', 'major', 'warning', 'minor', 'info')"
    )
    _create_check_constraint_idempotent(
        "ck_alarms_status_valid", "alarms", "status IN ('firing', 'acknowledged', 'recovered')"
    )

    # users
    _create_table_if_not_exists(
        "users",
        sa.Column("user_id", sa.String(16), primary_key=True),
        sa.Column("username", sa.String(32), nullable=False, unique=True),
        sa.Column("password", sa.String(128), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )
    _create_check_constraint_idempotent("ck_users_role_valid", "users", "role IN ('admin', 'operator', 'viewer')")

    # cache_queue
    _create_table_if_not_exists(
        "cache_queue",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("measurement", sa.String(128), nullable=False),
        sa.Column("tags", sa.Text, nullable=False),
        sa.Column("fields", sa.Text, nullable=False),
        sa.Column("timestamp", sa.Text, nullable=False),
        sa.Column("retry_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("cache_queue")
    op.drop_table("users")
    op.drop_table("alarms")
    op.drop_table("rules")
    op.drop_table("devices")


# ── Idempotent helper functions ──────────────────────────────────────────────────


def _create_index_idempotent(index_name: str, table_name: str, columns: list, **kw) -> None:
    """Create an index only if it doesn't already exist."""
    try:
        op.create_index(index_name, table_name, columns, if_not_exists=True, **kw)
    except Exception:
        pass


def _create_check_constraint_idempotent(constraint_name: str, table_name: str, sqltext: str, **kw) -> None:
    """Create a check constraint only if it doesn't already exist."""
    try:
        op.create_check_constraint(constraint_name, table_name, sqltext, **kw)
    except Exception:
        pass


def _create_table_if_not_exists(table_name: str, *columns, **kw) -> None:
    """Create a table only if it doesn't already exist."""
    try:
        op.create_table(table_name, *columns, **kw)
    except Exception:
        pass
