"""round6_7 schema changes: indexes, constraints, and column type fixes

Revision ID: 012_round6_7_schema_changes
Revises: 011_rule_script_columns
Create Date: 2026-06-20

# 第6-7轮修复的数据库 schema 变更:
#
# 1. RuleORM 添加 4 个索引 (device_id, enabled, severity, created_by)
#    原问题: 常用过滤字段无索引，规则查询全表扫描
#
# 2. AlarmORM 添加 ix_alarms_rule_id 索引
#    原问题: rule_id 无索引，告警关联规则查询全表扫描
#
# 3. 字段类型扩展 String(16) -> String(64)
#    - rules.rule_id (主键)
#    - alarms.alarm_id (主键)
#    - alarms.rule_id
#    - alarm_silences.rule_id
#    原问题: String(16) 截断 UUID/雪花ID，统一扩展为 String(64)
#
# 4. AiModelORM 添加约束与索引
#    - UniqueConstraint(model_name, model_version)
#    - Index(model_name)
#    - Index(status)
#    - CheckConstraint(model_type IN (...))
#    原问题: 无唯一约束可插入重复 name+version; 无索引查询全表扫描; 无类型约束
#
# 5. AiInferenceLogORM 添加复合索引 (model_id, timestamp)
#    原问题: 无复合索引，按模型+时间范围查询效率低
#    注意: ORM 字段名为 timestamp (任务描述中的 created_at 为笔误)
#
# 6. AiModelVersionORM 添加 UniqueConstraint(model_id, version)
#    原问题: 缺少唯一约束，可插入重复版本记录
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "012_round6_7_schema_changes"
down_revision: str | None = "011_rule_script_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ============================================================
    # 1. RuleORM 索引 (第6轮修复)
    # 原问题: RuleORM 常用过滤字段 device_id/enabled/severity/created_by 无索引，
    #         规则查询全表扫描
    # ============================================================
    _create_index_idempotent("idx_rules_device_id", "rules", ["device_id"])
    _create_index_idempotent("idx_rules_enabled", "rules", ["enabled"])
    _create_index_idempotent("idx_rules_severity", "rules", ["severity"])
    _create_index_idempotent("idx_rules_created_by", "rules", ["created_by"])

    # ============================================================
    # 2. AlarmORM 索引 (第6轮修复)
    # 原问题: AlarmORM.rule_id 无索引，告警关联规则查询全表扫描
    # ============================================================
    _create_index_idempotent("ix_alarms_rule_id", "alarms", ["rule_id"])

    # ============================================================
    # 3. AiModelORM 约束与索引 (第7轮修复)
    # 原问题: AiModelORM 无唯一约束可插入重复 name+version;
    #         无索引查询全表扫描; 无 model_type 约束可写入非法值
    # ============================================================
    _create_unique_constraint_idempotent("uq_ai_models_name_version", "ai_models", ["model_name", "model_version"])
    _create_index_idempotent("ix_ai_models_name", "ai_models", ["model_name"])
    _create_index_idempotent("ix_ai_models_status", "ai_models", ["status"])
    _create_check_constraint_idempotent(
        "ck_ai_models_type_valid",
        "ai_models",
        "model_type IN ('anomaly', 'trend', 'threshold', 'custom', 'unavailable')",
    )

    # ============================================================
    # 4. AiInferenceLogORM 复合索引 (第7轮修复)
    # 原问题: ai_inference_logs 无 (model_id, timestamp) 复合索引，
    #         按模型+时间范围查询效率低
    # 注意: ORM 中字段名为 timestamp (任务描述中的 created_at 为笔误)
    # ============================================================
    _create_index_idempotent("ix_ai_logs_model_time", "ai_inference_logs", ["model_id", "timestamp"])

    # ============================================================
    # 5. AiModelVersionORM 唯一约束 (第7轮修复)
    # 原问题: AiModelVersionORM 缺少 (model_id, version) 唯一约束，
    #         可插入重复版本记录
    # ============================================================
    _create_unique_constraint_idempotent("uq_ai_model_versions_id_ver", "ai_model_versions", ["model_id", "version"])

    # ============================================================
    # 6. 字段类型扩展 String(16) -> String(64) (第6轮修复)
    # 原问题: rule_id/alarm_id 为 String(16)，截断 UUID/雪花ID
    # 修复: 统一扩展为 String(64)
    # SQLite 特殊处理: SQLite 不支持直接 ALTER COLUMN，
    #   使用 batch_alter_table (会自动重建表并保留主键约束/索引)
    # ============================================================

    # rules.rule_id (主键) - batch 模式会自动保留 PRIMARY KEY 约束
    _alter_column_type_idempotent("rules", "rule_id", sa.String(16), sa.String(64))

    # alarms.alarm_id (主键)
    _alter_column_type_idempotent("alarms", "alarm_id", sa.String(16), sa.String(64))

    # alarms.rule_id
    _alter_column_type_idempotent("alarms", "rule_id", sa.String(16), sa.String(64))

    # alarm_silences.rule_id
    _alter_column_type_idempotent("alarm_silences", "rule_id", sa.String(16), sa.String(64))


def downgrade() -> None:
    # ============================================================
    # 6. 回滚字段类型 String(64) -> String(16)
    # FIXED(严重-R2): 原问题-若已有数据长度超过 16 字符，回滚会截断或失败导致数据丢失
    # 修复-回滚前检查是否存在超长数据，若有则拒绝回滚
    # ============================================================
    from sqlalchemy import text

    _downgrade_check_tables = [
        ("alarm_silences", "rule_id"),
        ("alarms", "rule_id"),
        ("alarms", "alarm_id"),
        ("rules", "rule_id"),
    ]
    bind = op.get_bind()
    for table, col in _downgrade_check_tables:
        try:
            result = bind.execute(text(f"SELECT COUNT(*) FROM {table} WHERE LENGTH({col}) > 16")).scalar()
            if result and result > 0:
                raise RuntimeError(
                    f"Cannot downgrade: {result} rows in {table}.{col} "
                    f"exceed 16 chars. Data loss would occur. "
                    f"Please truncate or migrate these records before downgrading."
                )
        except RuntimeError:
            raise
        except Exception:
            pass  # 表或列可能不存在，跳过检查

    _alter_column_type_idempotent("alarm_silences", "rule_id", sa.String(64), sa.String(16))
    _alter_column_type_idempotent("alarms", "rule_id", sa.String(64), sa.String(16))
    _alter_column_type_idempotent("alarms", "alarm_id", sa.String(64), sa.String(16))
    _alter_column_type_idempotent("rules", "rule_id", sa.String(64), sa.String(16))

    # ============================================================
    # 5. 回滚 AiModelVersionORM 唯一约束
    # ============================================================
    _drop_constraint_if_exists("uq_ai_model_versions_id_ver", "ai_model_versions", type_="unique")

    # ============================================================
    # 4. 回滚 AiInferenceLogORM 复合索引
    # ============================================================
    _drop_index_if_exists("ix_ai_logs_model_time", table_name="ai_inference_logs")

    # ============================================================
    # 3. 回滚 AiModelORM 约束与索引
    # ============================================================
    _drop_constraint_if_exists("ck_ai_models_type_valid", "ai_models")
    _drop_index_if_exists("ix_ai_models_status", table_name="ai_models")
    _drop_index_if_exists("ix_ai_models_name", table_name="ai_models")
    _drop_constraint_if_exists("uq_ai_models_name_version", "ai_models", type_="unique")

    # ============================================================
    # 2. 回滚 AlarmORM 索引
    # ============================================================
    _drop_index_if_exists("ix_alarms_rule_id", table_name="alarms")

    # ============================================================
    # 1. 回滚 RuleORM 索引
    # ============================================================
    _drop_index_if_exists("idx_rules_created_by", table_name="rules")
    _drop_index_if_exists("idx_rules_severity", table_name="rules")
    _drop_index_if_exists("idx_rules_enabled", table_name="rules")
    _drop_index_if_exists("idx_rules_device_id", table_name="rules")


# ── Idempotent helper functions ────────────────────────────────────────────────


def _create_index_idempotent(index_name: str, table_name: str, columns: list, **kw) -> None:
    """Create an index only if it doesn't already exist."""
    try:
        op.create_index(index_name, table_name, columns, if_not_exists=True, **kw)
    except Exception:
        # Index may already exist
        pass


def _create_unique_constraint_idempotent(constraint_name: str, table_name: str, columns: list, **kw) -> None:
    """Create a unique constraint only if it doesn't already exist."""
    try:
        op.create_unique_constraint(constraint_name, table_name, columns, **kw)
    except Exception:
        # Constraint may already exist
        pass


def _create_check_constraint_idempotent(constraint_name: str, table_name: str, sqltext: str, **kw) -> None:
    """Create a check constraint only if it doesn't already exist."""
    try:
        op.create_check_constraint(constraint_name, table_name, sqltext, **kw)
    except Exception:
        # Constraint may already exist
        pass


def _alter_column_type_idempotent(
    table_name: str,
    column_name: str,
    existing_type: sa.types.TypeEngine,
    new_type: sa.types.TypeEngine,
) -> None:
    """Alter a column type, using batch mode for SQLite compatibility.

    SQLite does not support ALTER COLUMN directly; batch_alter_table
    recreates the table with the new column type while preserving
    primary key constraints and indexes.
    """
    try:
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.alter_column(column_name, existing_type=existing_type, type_=new_type)
    except Exception:
        # Column type may already be the target type, or operation unsupported
        pass


def _drop_index_if_exists(index_name: str, table_name: str = None) -> None:
    """Drop an index only if it exists."""
    try:
        op.drop_index(index_name, table_name=table_name)
    except Exception:
        # Index may not exist
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
