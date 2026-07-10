"""unify rule_type CHECK constraint across rules/alarms/rule_templates

Revision ID: 013_unify_rule_type_constraint
Revises: 012_round6_7_schema_changes
Create Date: 2026-06-24

# 致命1修复: rule_type CHECK 约束三处不一致导致告警写入必失败
#
# 原问题:
#   - RuleORM: rule_type IN ('threshold', 'script', 'expression', 'ai')
#   - AlarmORM: rule_type IN ('threshold', 'ai_inference', 'trend')
#   - RuleTemplateORM: rule_type IN ('threshold', 'ai_inference', 'trend')
#   当 rule_type='script'（RuleORM合法）触发告警时，AlarmORM的CHECK约束拒绝写入
#
# 修复: 统一三处为 ('threshold', 'ai_inference', 'script')，与 Pydantic 对齐
#
# 数据兼容性处理:
#   - rules.rule_type: 'expression' → 'script', 'ai' → 'ai_inference'
#   - alarms.rule_type: 'trend' → 'threshold'
#   - rule_templates.rule_type: 'trend' → 'threshold'
#   - 其他非标准值 → 'threshold'（默认值）
#
# SQLite 不支持 ALTER CONSTRAINT，使用 batch_alter_table 重建表
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "013_unify_rule_type_constraint"
down_revision: str | None = "012_round6_7_schema_changes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# 统一后的合法 rule_type 值
_VALID_RULE_TYPES = "('threshold', 'ai_inference', 'script')"


def upgrade() -> None:
    bind = op.get_bind()

    # ============================================================
    # 1. 数据兼容性: 将非标准 rule_type 值转换为合法值
    #    必须在重建表之前执行，否则新约束会拒绝旧数据
    # ============================================================
    _normalize_rule_type_data(bind)

    # ============================================================
    # 2. 重建 rules 表的 rule_type CHECK 约束
    # ============================================================
    _rebuild_rule_type_constraint("rules", "ck_rules_rule_type_valid")

    # ============================================================
    # 3. 重建 alarms 表的 rule_type CHECK 约束
    # ============================================================
    _rebuild_rule_type_constraint("alarms", "ck_alarms_rule_type_valid")

    # ============================================================
    # 4. 重建 rule_templates 表的 rule_type CHECK 约束
    # ============================================================
    _rebuild_rule_type_constraint("rule_templates", "ck_rule_templates_type_valid")


def downgrade() -> None:
    # 回滚: 恢复旧的 CHECK 约束（不回滚数据，因为旧值可能已被转换）
    _rebuild_constraint_with_sqltext(
        "rules", "ck_rules_rule_type_valid",
        "rule_type IN ('threshold', 'script', 'expression', 'ai')",
    )
    _rebuild_constraint_with_sqltext(
        "alarms", "ck_alarms_rule_type_valid",
        "rule_type IN ('threshold', 'ai_inference', 'trend')",
    )
    _rebuild_constraint_with_sqltext(
        "rule_templates", "ck_rule_templates_type_valid",
        "rule_type IN ('threshold', 'ai_inference', 'trend')",
    )


# ── Helper functions ──────────────────────────────────────────────────────────


def _normalize_rule_type_data(bind) -> None:
    """将非标准 rule_type 值转换为合法值，确保数据兼容新约束。"""
    inspector = sa.inspect(bind)
    try:
        existing_tables = inspector.get_table_names()
    except Exception:
        existing_tables = []

    # rules 表: 'expression' → 'script', 'ai' → 'ai_inference'
    if "rules" in existing_tables:
        try:
            bind.execute(sa.text(
                "UPDATE rules SET rule_type='script' WHERE rule_type='expression'"
            ))
            bind.execute(sa.text(
                "UPDATE rules SET rule_type='ai_inference' WHERE rule_type='ai'"
            ))
            # 兜底: 其他非标准值 → 'threshold'
            bind.execute(sa.text(
                "UPDATE rules SET rule_type='threshold' "
                "WHERE rule_type NOT IN ('threshold', 'ai_inference', 'script')"
            ))
        except Exception:
            pass

    # alarms 表: 'trend' → 'threshold'
    if "alarms" in existing_tables:
        try:
            bind.execute(sa.text(
                "UPDATE alarms SET rule_type='threshold' WHERE rule_type='trend'"
            ))
            bind.execute(sa.text(
                "UPDATE alarms SET rule_type='threshold' "
                "WHERE rule_type NOT IN ('threshold', 'ai_inference', 'script')"
            ))
        except Exception:
            pass

    # rule_templates 表: 'trend' → 'threshold'
    if "rule_templates" in existing_tables:
        try:
            bind.execute(sa.text(
                "UPDATE rule_templates SET rule_type='threshold' WHERE rule_type='trend'"
            ))
            bind.execute(sa.text(
                "UPDATE rule_templates SET rule_type='threshold' "
                "WHERE rule_type NOT IN ('threshold', 'ai_inference', 'script')"
            ))
        except Exception:
            pass


def _rebuild_rule_type_constraint(table_name: str, constraint_name: str) -> None:
    """使用 batch_alter_table 重建表的 rule_type CHECK 约束。

    SQLite 不支持 ALTER CONSTRAINT，batch_alter_table 会自动重建表。
    幂等: 如果约束已是目标值则跳过。
    """
    sqltext = f"rule_type IN {_VALID_RULE_TYPES}"

    # 检查是否需要修复: 读取表的 CREATE SQL 检查约束文本
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        try:
            result = bind.execute(sa.text(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=:t"
            ), {"t": table_name})
            row = result.fetchone()
            if row and row[0]:
                table_sql = row[0]
                # 如果约束文本已包含 'script' 且不包含 'trend'/'expression'/'ai'，
                # 说明已是目标约束，跳过
                if "'script'" in table_sql and "'trend'" not in table_sql and "'expression'" not in table_sql:
                    return
        except Exception:
            pass

    try:
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.drop_constraint(constraint_name, type_="check")
            batch_op.create_check_constraint(constraint_name, sqltext)
    except Exception:
        # 约束可能不存在或已是目标值，尝试仅创建
        try:
            with op.batch_alter_table(table_name) as batch_op:
                batch_op.create_check_constraint(constraint_name, sqltext)
        except Exception:
            pass


def _rebuild_constraint_with_sqltext(
    table_name: str, constraint_name: str, sqltext: str
) -> None:
    """downgrade 用: 用指定 sqltext 重建约束。"""
    try:
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.drop_constraint(constraint_name, type_="check")
            batch_op.create_check_constraint(constraint_name, sqltext)
    except Exception:
        pass
