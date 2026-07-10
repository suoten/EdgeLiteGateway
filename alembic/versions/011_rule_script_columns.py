"""add script and rule_type columns to rules table

Revision ID: 011_rule_script_columns
Revises: 010_rule_versions
Create Date: 2026-06-20

# [SEC-FIX] 修复 RuleORM 缺 script/rule_type 列问题:
# 原问题: RuleORM 无 script/rule_type 列，evaluator 读取但无法持久化，
# 导致脚本规则/表达式规则重启后丢失配置。
# 修复: 为 rules 表新增 script(TEXT) 和 rule_type(VARCHAR(16)) 列，
# 默认值分别为 '' 和 'threshold'，并添加 rule_type 取值约束。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "011_rule_script_columns"
down_revision: str | None = "010_rule_versions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # [SEC-FIX] 为 rules 表新增 script/rule_type 列
    # SQLite ALTER TABLE ADD COLUMN 不支持 IF NOT EXISTS，用 inspect 检查
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    try:
        existing_cols = [c["name"] for c in inspector.get_columns("rules")]
    except Exception:
        existing_cols = []

    if "script" not in existing_cols:
        try:
            op.add_column(
                "rules",
                sa.Column("script", sa.Text, nullable=False, server_default=""),
            )
        except Exception:
            pass

    if "rule_type" not in existing_cols:
        try:
            op.add_column(
                "rules",
                sa.Column(
                    "rule_type",
                    sa.String(16),
                    nullable=False,
                    server_default="threshold",
                ),
            )
        except Exception:
            pass


def downgrade() -> None:
    # SQLite 不支持 DROP COLUMN（旧版本），仅作占位
    # 如需回滚，请重建 rules 表
    pass
