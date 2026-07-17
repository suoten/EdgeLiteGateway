"""add missing tables: device_templates, resource_shares, revoked_tokens,
login_attempts, account_lockouts, global_login_failures,
global_account_lockouts

Also adds missing columns to existing tables (devices, rules, alarms, users)
that were present in the ORM but omitted from 001_initial_schema.py.

NOTE: Password reset rate-limiting tables (password_reset_attempts,
password_reset_user_rates, used_password_reset_tokens,
password_reset_ip_attempts) are created in migration 005.

This migration is idempotent - it uses if_not_exists and try/except blocks
to handle databases that were previously upgraded via manual _migrate() code.

Revision ID: 004_missing_tables
Revises: 003_ai_model_versions
Create Date: 2026-06-03
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "004_missing_tables"
down_revision: str | None = "003_ai_model_versions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── Helper to make operations idempotent ────────────────────────────────────
    # These tables/columns may already exist from manual _migrate() code

    # ── Missing columns on existing tables ────────────────────────────────────
    # We use batch_alter_table with try/except to handle idempotency
    # batch_alter_table is required for SQLite and other backends

    # devices: add ORM-defined columns that 001 missed
    _add_column_idempotent("devices", sa.Column("created_by", sa.String(64), nullable=True))
    _add_column_idempotent("devices", sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
    _add_column_idempotent("devices", sa.Column("updated_at", sa.DateTime(), nullable=True))
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

    # rules: add ORM-defined columns
    _add_column_idempotent("rules", sa.Column("created_by", sa.String(64), nullable=True))
    _add_column_idempotent("rules", sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
    _create_check_constraint_idempotent("ck_rules_logic_valid", "rules", "logic IN ('AND', 'OR', 'NOT')")
    _create_check_constraint_idempotent(
        "ck_rules_severity_valid", "rules", "severity IN ('critical', 'major', 'warning', 'minor', 'info')"
    )
    _create_check_constraint_idempotent("ck_rules_duration_non_negative", "rules", "duration >= 0")

    # alarms: add ORM-defined columns (message, rule_type, version)
    _add_column_idempotent("alarms", sa.Column("message", sa.String(256), nullable=False, server_default=""))
    _add_column_idempotent("alarms", sa.Column("rule_type", sa.String(32), nullable=False, server_default="threshold"))
    _add_column_idempotent("alarms", sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
    _create_check_constraint_idempotent(
        "ck_alarms_severity_valid", "alarms", "severity IN ('critical', 'major', 'warning', 'minor', 'info')"
    )
    _create_check_constraint_idempotent(
        "ck_alarms_status_valid", "alarms", "status IN ('firing', 'acknowledged', 'recovered')"
    )
    _create_check_constraint_idempotent(
        "ck_alarms_rule_type_valid", "alarms", "rule_type IN ('threshold', 'ai_inference', 'trend')"
    )

    # users: add ORM-defined columns (must_change_password, password_changed_at, updated_at, version)
    _add_column_idempotent("users", sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default="0"))
    _add_column_idempotent("users", sa.Column("password_changed_at", sa.DateTime(), nullable=True))
    _add_column_idempotent("users", sa.Column("updated_at", sa.DateTime(), nullable=True))
    _add_column_idempotent("users", sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
    _create_check_constraint_idempotent("ck_users_role_valid", "users", "role IN ('admin', 'operator', 'viewer')")

    # cache_queue: add ORM-defined status column and index
    _add_column_idempotent("cache_queue", sa.Column("status", sa.String(16), nullable=False, server_default="pending"))
    _create_index_idempotent("idx_cache_queue_status", "cache_queue", ["status"])

    # ── New tables ────────────────────────────────────────────────────────────

    # device_templates
    _create_table_if_not_exists(
        "device_templates",
        sa.Column("name", sa.String(64), primary_key=True),
        sa.Column("protocol", sa.String(32), nullable=False),
        sa.Column("config_template", sa.Text, nullable=False, server_default="{}"),
        sa.Column("point_templates", sa.Text, nullable=False, server_default="[]"),
        sa.Column("created_by", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )
    # FIXED-P0: 表已存在时 _create_table_if_not_exists 不会添加缺失列, 需单独补加
    _add_column_idempotent("device_templates", sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
    _create_check_constraint_idempotent(
        "ck_device_templates_protocol_valid",
        "device_templates",
        "protocol IN ("
        "'modbus_tcp', 'modbus_rtu', 'simulator', 'mqtt_client', 'http_webhook', "
        "'opc_ua', 'siemens_s7', 'mitsubishi_mc', 'omron_fins', 'allen_bradley', "
        "'opc_da', 'onvif', 'video_ai', 'modbus_slave'"
        ")",
    )

    # resource_shares
    _create_table_if_not_exists(
        "resource_shares",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("resource_type", sa.String(32), nullable=False),
        sa.Column("resource_id", sa.String(64), nullable=False),
        sa.Column("shared_with_user_id", sa.String(64), nullable=False),
        sa.Column("permission_level", sa.String(16), nullable=False, server_default="read"),
        sa.Column("shared_by_user_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )
    _create_index_idempotent(
        "idx_resource_shares_lookup", "resource_shares", ["resource_type", "resource_id", "shared_with_user_id"]
    )
    _create_index_idempotent("idx_resource_shares_user", "resource_shares", ["shared_with_user_id"])
    _create_unique_constraint_idempotent(
        "uq_resource_shares_unique", "resource_shares", ["resource_type", "resource_id", "shared_with_user_id"]
    )

    # revoked_tokens  (FIXED-P0: token revocation persistence)
    _create_table_if_not_exists(
        "revoked_tokens",
        sa.Column("jti", sa.String(64), primary_key=True),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), server_default=sa.func.now()),
    )
    _create_index_idempotent("idx_revoked_tokens_expires", "revoked_tokens", ["expires_at"])

    # login_attempts  (FIXED-H03: persistent login rate limiting)
    _create_table_if_not_exists(
        "login_attempts",
        sa.Column("ip", sa.String(45), primary_key=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_attempt_at", sa.Float(), nullable=False),
        sa.Column("last_attempt_at", sa.Float(), nullable=False),
    )
    _create_index_idempotent("idx_login_attempts_last", "login_attempts", ["last_attempt_at"])

    # account_lockouts
    _create_table_if_not_exists(
        "account_lockouts",
        sa.Column("lockout_key", sa.String(128), primary_key=True),
        sa.Column("username", sa.String(32), nullable=False),
        sa.Column("ip", sa.String(45), nullable=False),
        sa.Column("lockout_until", sa.Float(), nullable=False),
        sa.Column("fail_count", sa.Integer(), nullable=False, server_default="0"),
    )
    _create_index_idempotent("idx_account_lockouts_user", "account_lockouts", ["username"])
    _create_index_idempotent("idx_account_lockouts_until", "account_lockouts", ["lockout_until"])

    # global_login_failures  (FIXED-M03: global failure tracking)
    _create_table_if_not_exists(
        "global_login_failures",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("timestamp", sa.Float(), nullable=False),
        sa.Column("username", sa.String(32), nullable=True),
        sa.Column("ip", sa.String(45), nullable=True),
    )
    _create_index_idempotent("idx_global_failures_ts", "global_login_failures", ["timestamp"])

    # global_account_lockouts  (FIXED-M03: username-level lockout)
    _create_table_if_not_exists(
        "global_account_lockouts",
        sa.Column("username", sa.String(32), primary_key=True),
        sa.Column("fail_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_attempt_at", sa.Float(), nullable=False),
        sa.Column("last_attempt_at", sa.Float(), nullable=False),
        sa.Column("locked_until", sa.Float(), nullable=False, server_default="0"),
    )
    _create_index_idempotent("idx_global_lockouts_until", "global_account_lockouts", ["locked_until"])


def downgrade() -> None:
    # NOTE: Password reset tables (005) are dropped separately
    # Drop new tables in reverse order of creation

    _drop_index_if_exists("idx_global_lockouts_until", table_name="global_account_lockouts")
    op.drop_table("global_account_lockouts")

    _drop_index_if_exists("idx_global_failures_ts", table_name="global_login_failures")
    op.drop_table("global_login_failures")

    _drop_index_if_exists("idx_account_lockouts_until", table_name="account_lockouts")
    _drop_index_if_exists("idx_account_lockouts_user", table_name="account_lockouts")
    op.drop_table("account_lockouts")

    _drop_index_if_exists("idx_login_attempts_last", table_name="login_attempts")
    op.drop_table("login_attempts")

    _drop_index_if_exists("idx_revoked_tokens_expires", table_name="revoked_tokens")
    op.drop_table("revoked_tokens")

    _drop_constraint_if_exists("uq_resource_shares_unique", "resource_shares", type_="unique")
    _drop_index_if_exists("idx_resource_shares_user", table_name="resource_shares")
    _drop_index_if_exists("idx_resource_shares_lookup", table_name="resource_shares")
    op.drop_table("resource_shares")

    _drop_constraint_if_exists("ck_device_templates_protocol_valid", "device_templates")
    op.drop_table("device_templates")

    # Drop restored ORM columns from existing tables (reverse of upgrade)
    _drop_index_if_exists("idx_cache_queue_status", table_name="cache_queue")
    _drop_column_if_exists("cache_queue", "status")

    _drop_constraint_if_exists("ck_users_role_valid", "users")
    _drop_column_if_exists("users", "version")
    _drop_column_if_exists("users", "updated_at")
    _drop_column_if_exists("users", "password_changed_at")
    _drop_column_if_exists("users", "must_change_password")

    _drop_constraint_if_exists("ck_alarms_rule_type_valid", "alarms")
    _drop_constraint_if_exists("ck_alarms_status_valid", "alarms")
    _drop_constraint_if_exists("ck_alarms_severity_valid", "alarms")
    _drop_column_if_exists("alarms", "version")
    _drop_column_if_exists("alarms", "rule_type")
    _drop_column_if_exists("alarms", "message")

    _drop_constraint_if_exists("ck_rules_duration_non_negative", "rules")
    _drop_constraint_if_exists("ck_rules_severity_valid", "rules")
    _drop_constraint_if_exists("ck_rules_logic_valid", "rules")
    _drop_column_if_exists("rules", "version")
    _drop_column_if_exists("rules", "created_by")

    _drop_constraint_if_exists("ck_devices_protocol_valid", "devices")
    _drop_constraint_if_exists("ck_devices_status_valid", "devices")
    _drop_constraint_if_exists("ck_devices_collect_interval_positive", "devices")
    _drop_index_if_exists("idx_devices_created_by", table_name="devices")
    _drop_column_if_exists("devices", "updated_at")
    _drop_column_if_exists("devices", "version")
    _drop_column_if_exists("devices", "created_by")


# ── Idempotent helper functions ────────────────────────────────────────────────


def _add_column_idempotent(table_name: str, column: sa.Column) -> None:
    """Add a column only if it doesn't already exist."""
    try:
        with op.batch_alter_table(table_name) as batch:
            batch.add_column(column)
    except Exception:
        # Column may already exist (e.g., from manual _migrate() or previous migration run)
        pass


def _create_index_idempotent(index_name: str, table_name: str, columns: list, **kw) -> None:
    """Create an index only if it doesn't already exist."""
    try:
        op.create_index(index_name, table_name, columns, if_not_exists=True, **kw)
    except Exception:
        # Index may already exist
        pass


def _create_check_constraint_idempotent(constraint_name: str, table_name: str, sqltext: str, **kw) -> None:
    """Create a check constraint only if it doesn't already exist."""
    try:
        op.create_check_constraint(constraint_name, table_name, sqltext, **kw)
    except Exception:
        # Constraint may already exist
        pass


def _create_unique_constraint_idempotent(constraint_name: str, table_name: str, columns: list, **kw) -> None:
    """Create a unique constraint only if it doesn't already exist."""
    try:
        op.create_unique_constraint(constraint_name, table_name, columns, **kw)
    except Exception:
        # Constraint may already exist - check if it's a duplicate
        pass


def _create_table_if_not_exists(table_name: str, *columns, **kw) -> None:
    """Create a table only if it doesn't already exist."""
    try:
        op.create_table(table_name, *columns, **kw)
    except Exception:
        # Table may already exist
        pass


def _drop_index_if_exists(index_name: str, table_name: str = None) -> None:
    """Drop an index only if it exists."""
    try:
        op.drop_index(index_name, table_name=table_name)
    except Exception:
        # Index may not exist
        pass


def _drop_column_if_exists(table_name: str, column_name: str) -> None:
    """Drop a column only if it exists."""
    try:
        with op.batch_alter_table(table_name) as batch:
            batch.drop_column(column_name)
    except Exception:
        # Column may not exist
        pass


def _drop_constraint_if_exists(constraint_name: str, table_name: str, type_: str = None) -> None:
    """Drop a constraint only if it exists."""
    try:
        if type_:
            op.drop_constraint(constraint_name, table_name, type_=type_)
        else:
            op.drop_constraint(constraint_name, table_name)
    except Exception:
        # Constraint may not exist
        pass
