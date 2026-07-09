"""add updated_at column to rules table

Revision ID: 007_rules_add_updated_at
Revises: 006_rule_templates_device_groups
Create Date: 2026-06-19

#[AUDIT-FIX] 修复 rules 表缺失 updated_at 列导致 RuleRepo.list 每分钟报错:
  sqlite3.OperationalError: no such column: rules.updated_at

根因:
- 001_initial_schema 创建 rules 表时只有 created_at，无 updated_at
- 004_missing_tables 给 rules 添加了 created_by 和 version，但漏掉了 updated_at
- ORM 模型 RuleORM (db.py:68) 有 updated_at 字段，与数据库 schema 不一致

本迁移为 rules 表补充 updated_at 列，与 DeviceORM/UserORM 保持一致。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "007_rules_add_updated_at"
down_revision: Union[str, None] = "006_rule_templates_device_groups"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # #[AUDIT-FIX] 为 rules 表添加 updated_at 列
    # nullable=True 兼容已有数据行（无更新时间），新更新会由 ORM onupdate=_utcnow 自动填充
    try:
        with op.batch_alter_table("rules") as batch:
            batch.add_column(sa.Column("updated_at", sa.DateTime(), nullable=True))
    except Exception:
        # 列可能已存在（手动迁移或重复执行），忽略
        pass


def downgrade() -> None:
    try:
        with op.batch_alter_table("rules") as batch:
            batch.drop_column("updated_at")
    except Exception:
        pass
