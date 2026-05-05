"""initial schema

Revision ID: 001
Revises:
Create Date: 2024-01-01 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
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

    op.create_table(
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

    op.create_table(
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
    op.create_index("idx_alarms_status", "alarms", ["status"])
    op.create_index("idx_alarms_device", "alarms", ["device_id"])

    op.create_table(
        "users",
        sa.Column("user_id", sa.String(16), primary_key=True),
        sa.Column("username", sa.String(32), nullable=False, unique=True),
        sa.Column("password", sa.String(128), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )

    op.create_table(
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
