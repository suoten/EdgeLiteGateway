"""add ai_inference_logs indexes and user_sessions table

Revision ID: 009_ai_logs_indexes_and_sessions
Revises: 008_alarm_silences_table
Create Date: 2026-06-20

# [AUDIT-FIX] 修复两个问题:
# 1. ai_inference_logs 表缺少索引，ai_service 启动时 GROUP BY model_id /
#    WHERE status='error' GROUP BY model_id 等聚合查询全表扫描，日志增长后
#    严重影响启动速度。添加 model_id、status、timestamp 三个索引。
# 2. session_manager 原 fail-open 策略：重启后内存状态丢失，并发登录控制和
#    token 撤销机制双重失效。新增 user_sessions 表持久化会话状态。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "009_ai_logs_indexes_and_sessions"
down_revision: Union[str, None] = "008_alarm_silences_table"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. ai_inference_logs 索引 (FIXED: add if_not_exists to handle pre-existing indexes)
    op.create_index("idx_ai_logs_model", "ai_inference_logs", ["model_id"], if_not_exists=True)
    op.create_index("idx_ai_logs_status", "ai_inference_logs", ["status"], if_not_exists=True)
    op.create_index("idx_ai_logs_timestamp", "ai_inference_logs", ["timestamp"], if_not_exists=True)

    # 2. user_sessions 表（会话持久化）
    op.create_table(
        "user_sessions",
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("jti", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.Float, nullable=False),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.current_timestamp()),
        sa.PrimaryKeyConstraint("jti"),
    )
    op.create_index("idx_user_sessions_user", "user_sessions", ["user_id"], if_not_exists=True)
    op.create_index("idx_user_sessions_expires", "user_sessions", ["expires_at"], if_not_exists=True)


def downgrade() -> None:
    op.drop_index("idx_user_sessions_expires", table_name="user_sessions")
    op.drop_index("idx_user_sessions_user", table_name="user_sessions")
    op.drop_table("user_sessions")

    op.drop_index("idx_ai_logs_timestamp", table_name="ai_inference_logs")
    op.drop_index("idx_ai_logs_status", table_name="ai_inference_logs")
    op.drop_index("idx_ai_logs_model", table_name="ai_inference_logs")
