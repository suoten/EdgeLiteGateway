"""create alarm_silences table

Revision ID: 008_alarm_silences_table
Revises: 007_rules_add_updated_at
Create Date: 2026-06-19

#[AUDIT-FIX] 修复 alarm_silences 表缺失导致 list_silences 失败:
  ERROR | edgelite.services.alarm_silence | list_silences failed:

根因:
- AlarmSilenceORM (db.py:79) 定义了 alarm_silences 表
- 但该表只能通过手动运行 `python -m edgelite.services.migrate_alarm_silence` 创建
- 该脚本不在 bootstrap 中自动调用，也不在 Alembic 迁移中
- 新安装/新数据库中该表不存在，导致所有告警静默查询失败

本迁移在主数据库中创建 alarm_silences 表，与 ORM 模型定义一致。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "008_alarm_silences_table"
down_revision: str | None = "007_rules_add_updated_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # #[AUDIT-FIX] 创建 alarm_silences 表，与 AlarmSilenceORM (db.py:79) 一致
    try:
        op.create_table(
            "alarm_silences",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("device_id", sa.String(64), nullable=True),
            sa.Column("rule_id", sa.String(16), nullable=True),
            sa.Column("start_time", sa.DateTime, nullable=False),
            sa.Column("end_time", sa.DateTime, nullable=False),
            sa.Column("reason", sa.String(256), nullable=False, server_default=""),
            sa.Column("operator", sa.String(64), nullable=False, server_default="system"),
            sa.Column("created_at", sa.DateTime, nullable=False),
        )
        op.create_index("idx_alarm_silences_device", "alarm_silences", ["device_id"])
        op.create_index("idx_alarm_silences_rule", "alarm_silences", ["rule_id"])
        op.create_index("idx_alarm_silences_window", "alarm_silences", ["start_time", "end_time"])
    except Exception:
        # 表可能已存在（手动迁移或重复执行），忽略
        pass


def downgrade() -> None:
    try:
        op.drop_index("idx_alarm_silences_window", table_name="alarm_silences")
        op.drop_index("idx_alarm_silences_rule", table_name="alarm_silences")
        op.drop_index("idx_alarm_silences_device", table_name="alarm_silences")
        op.drop_table("alarm_silences")
    except Exception:
        pass
