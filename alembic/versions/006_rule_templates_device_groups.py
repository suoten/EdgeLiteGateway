"""add rule_templates and device_groups tables

Revision ID: 006_rule_templates_device_groups
Revises: 005_password_reset_tables
Create Date: 2026-06-04

FIXED-P2: 原问题-RuleTemplate/DeviceGroup纯内存存储，进程重启丢失
改为ORM持久化，添加对应数据库表。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "006_rule_templates_device_groups"
down_revision: str | None = "005_password_reset_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    _create_table_if_not_exists(
        "rule_templates",
        sa.Column("template_id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("rule_type", sa.String(32), nullable=False, server_default="threshold"),
        sa.Column("default_conditions", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("default_severity", sa.String(16), nullable=False, server_default="warning"),
        sa.Column("default_duration", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("notify_channels", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("created_by", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )  # FIXED-P1: 表已存在时CheckConstraint不会单独创建，需在下方补加
    _create_check_constraint_idempotent(
        "ck_rule_templates_severity_valid", "rule_templates",
        "default_severity IN ('info', 'warning', 'error', 'critical')"
    )
    _create_check_constraint_idempotent(
        "ck_rule_templates_type_valid", "rule_templates",
        "rule_type IN ('threshold', 'range', 'rate', 'composite', 'trend', 'ai_anomaly')"
    )

    _create_table_if_not_exists(
        "device_groups",
        sa.Column("group_id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("parent_id", sa.String(64), nullable=True),
        sa.Column("device_ids", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("tags", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    _create_index_idempotent("idx_device_groups_parent", "device_groups", ["parent_id"])


def downgrade() -> None:
    _drop_index_if_exists("idx_device_groups_parent", table_name="device_groups")
    op.drop_table("device_groups")
    op.drop_table("rule_templates")


def _create_check_constraint_idempotent(constraint_name: str, table_name: str, sqltext: str, **kw) -> None:
    """Create a check constraint only if it doesn't already exist."""  # FIXED-P1
    try:
        op.create_check_constraint(constraint_name, table_name, sqltext, **kw)
    except Exception:
        pass


def _create_index_idempotent(index_name: str, table_name: str, columns: list, **kw) -> None:
    try:
        op.create_index(index_name, table_name, columns, if_not_exists=True, **kw)
    except Exception:
        pass


def _create_table_if_not_exists(table_name: str, *columns, **kw) -> None:
    try:
        op.create_table(table_name, *columns, **kw)
    except Exception:
        pass


def _drop_index_if_exists(index_name: str, table_name: str = None) -> None:
    try:
        op.drop_index(index_name, table_name=table_name)
    except Exception:
        pass
