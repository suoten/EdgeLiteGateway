"""create rule_versions table for API rule version history

Revision ID: 010_rule_versions
Revises: 009_ai_logs_indexes_and_sessions
Create Date: 2026-06-20

# [SEC-FIX-RULE-VERSION] 修复 API 规则系统无版本历史问题:
# 原问题: API 规则系统只有乐观锁 version 字段，无版本快照表，
# 误删/恶意修改后无法回滚。
# 修复: 参考 drivers/rule_store.py 的 rule_versions 表设计，
# 为主库 rules 表新增 rule_versions 版本快照表，支持版本历史查询与回滚。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "010_rule_versions"
down_revision: str | None = "009_ai_logs_indexes_and_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # [SEC-FIX-RULE-VERSION] 创建 rule_versions 表，与 RuleVersionORM (db.py) 一致
    try:
        op.create_table(
            "rule_versions",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("rule_id", sa.String(64), nullable=False),
            sa.Column("version", sa.Integer, nullable=False),
            sa.Column("snapshot", sa.Text, nullable=False),
            sa.Column("snapshot_hash", sa.String(64), nullable=False),
            sa.Column("change_summary", sa.String(256), nullable=True),
            sa.Column("created_by", sa.String(64), nullable=True),
            sa.Column("created_at", sa.DateTime, nullable=False),
            sa.UniqueConstraint("rule_id", "version", name="uq_rule_version"),
        )
        op.create_index("ix_rule_versions_rule_id", "rule_versions", ["rule_id"])
        op.create_index("idx_rule_versions_rule", "rule_versions", ["rule_id", "version"])
    except Exception:
        # 表可能已存在（重复执行），忽略
        pass


def downgrade() -> None:
    try:
        op.drop_index("idx_rule_versions_rule", table_name="rule_versions")
        op.drop_index("ix_rule_versions_rule_id", table_name="rule_versions")
        op.drop_table("rule_versions")
    except Exception:
        pass
